# -*- coding: utf-8 -*-

import time
import csv
import hashlib
import logging
import coloredlogs
import pickle
from multiprocessing import Process
coloredlogs.install(level=logging.INFO, milliseconds=True)

from collections import namedtuple, defaultdict

import mxnet as mx
import numpy as np
import cv2
import bson
import zmq

Batch = namedtuple('Batch', ['data'])


def read_csv_category(csv_path):
    cate2cid, cid2cate = dict(), dict()
    with open(csv_path, 'r') as reader:
        csvreader = csv.reader(reader, delimiter=',', quotechar='"')
        for i, row in enumerate(csvreader):
            if i == 0:
                continue
            try:
                cateid, cate1, cate2, cate3 = row
                cid = len(cate2cid)
                cate2cid[int(cateid)] = cid
                cid2cate[cid] = int(cateid)
            except Exception as e:
                print('cannot parse line: {}, {}'.format(row, e))
    print('read {} categories'.format(len(cate2cid)))
    return cate2cid, cid2cate


def read_images(bson_path, csv_path):
    cate_dict, _ = read_csv_category(csv_path)

    data = bson.decode_file_iter(open(bson_path, 'rb'))

    product_count, image_count = 0, 0
    for c, d in enumerate(data):
        product_id = d.get('_id')
        category_id = d.get('category_id', None)  # This won't be in Test data
        items = []
        for e, pic in enumerate(d['imgs']):
            picture = pic['picture']
            item = (image_count, picture, cate_dict[category_id] if category_id else product_id)
            items.append(item)
            image_count += 1
        product_count += 1
        yield items  # list of [id, picture, label, [label,]]
    logging.info('read finished (product:{}, image:{})'.format(product_count, image_count))


class Tester(object):
    def __init__(self, symbol_path, params_path, data_shape, mean_max_pooling=False, pool_name=None, fc_name=None, device_type='gpu', gpus='0'):
        self._data_shape = data_shape
        self._arg_params, self._aux_params = {}, {}
        save_dict = mx.nd.load(params_path)
        for k, v in save_dict.items():
            tp, name = k.split(':', 1)
            if tp == 'arg':
                self._arg_params[name] = v
            if tp == 'aux':
                self._aux_params[name] = v

        if mean_max_pooling:
            logging.info('Add Mean-Max Pooling Layer..')
            sym_softmax = mx.symbol.load(symbol_path)
            sym_pool, sym_fc = None, None
            for x in sym_softmax.get_internals():
                if x.name == pool_name:
                    sym_pool = x
                elif x.name == fc_name:
                    sym_fc = x

            if sym_pool is None or sym_fc is None:
                print( [x.name for x in sym_softmax.get_internals()])
                if sym_pool is None:
                    raise ValueError('Cannot find output that matches name {}'.format(sym_pool))
                if sym_fc is None:
                    raise ValueError('Cannot find output that matches name {}'.format(sym_fc))

            num_classes = sym_fc.attr('num_hidden')

            sym = mx.symbol.Convolution(data=sym_pool, num_filter=num_classes, kernel=(1, 1), no_bias=False, name=fc_name)
            fc_weight_name, fc_bias_name = fc_name + '_weight', fc_name + '_bias'
            fc_weight, fc_bias = self._arg_params[fc_weight_name], self._arg_params[fc_bias_name]
            self._arg_params[fc_weight_name] = fc_weight.reshape(fc_weight.shape + (1, 1))
            self._arg_params[fc_bias_name] = fc_bias.reshape(fc_bias.shape)

            mean_max_pooling_size = tuple([max(i // 32 - 6, 1) for i in data_shape[2:4]])

            sym1 = mx.symbol.Flatten(data=mx.symbol.Pooling(
                data=sym, global_pool=True, pool_type='avg', kernel=mean_max_pooling_size, stride=(1, 1), pad=(0, 0), name='out_pool1'))
            sym2 = mx.symbol.Flatten(data=mx.symbol.Pooling(
                data=sym, global_pool=True, pool_type='max', kernel=mean_max_pooling_size, stride=(1, 1), pad=(0, 0), name='out_pool2'))
            sym = (sym1 + sym2) * 0.5
            sym = mx.symbol.SoftmaxOutput(data=sym, name='softmax')
            self._symbol = sym
        else:
            self._symbol = mx.symbol.load(symbol_path)

        ctx = [mx.gpu(int(x)) for x in gpus.split(',')] if device_type == 'gpu' else mx.cpu()

        self._module = mx.mod.Module(symbol=self._symbol, label_names=None, context=ctx)
        self._module.bind(data_shapes=[('data', self._data_shape)],
                          label_shapes=None,
                          for_training=False)
        self._module.set_params(self._arg_params, self._aux_params, allow_missing=True)

    def get_output(self, batch_data):
        self._module.forward(Batch([mx.nd.array(batch_data)]), is_train=False)
        output = self._module.get_outputs()
        return output


def _reader(args):
    context = zmq.Context()
    zmq_socket = context.socket(zmq.PUSH)
    zmq_socket.set_hwm(1)
    zmq_socket.bind('tcp://0.0.0.0:{port}'.format(port=args.zmq_port))
    logging.info('reader started (port: {port})'.format(port=args.zmq_port))

    product_count = 0
    for items in read_images(args.bson, args.csv):
        zmq_socket.send_pyobj(items)
        product_count += 1

    for _ in range(args.num_procs):
        zmq_socket.send_pyobj(None)

    logging.info('reader finished (product: {})'.format(product_count))


def _do_forward(tester, batch_data, batch_ids, batch_raw, md5_dict=None, cate2cid=None):
    output = tester.get_output(batch_data)
    probs_dict = dict()
    probs = output[0].asnumpy()
    for i, _id in enumerate(batch_ids):
        if _id is not None:
            p = probs[i]
            if md5_dict:
                assert isinstance(cate2cid, dict)
                h = hashlib.md5(batch_raw[i]).hexdigest()
                if h in md5_dict:
                    p = np.zeros(probs.shape[1:])
                    p[cate2cid[md5_dict[h]]] = 1.0

            if _id in probs_dict:
                probs_dict[_id] += p
            else:
                probs_dict[_id] = p
    return probs_dict


def _write_output(args, probs_dict, cid2cate):
    for _id, prob in probs_dict.items():
        args.output.write('{0:d},{1:d}\n'.format(_id, cid2cate[int(np.argmax(prob))]))
        args.output.flush()


def _predict(args):
    cate2cid, cid2cate = read_csv_category(args.csv)
    md5_dict = pickle.load(open(args.md5_dict_pkl, 'rb')) if args.md5_dict_pkl else None

    data_shape = [int(x) for x in args.data_shape.split(',')]
    batch_shape = [args.batch_size] + data_shape
    logging.info('batch_shape: {}'.format(batch_shape))
    tester = Tester(args.symbol, args.params, batch_shape,
                    mean_max_pooling=args.mean_max_pooling, pool_name=args.pool_name, fc_name=args.fc_name,
                    gpus=args.gpus)

    context = zmq.Context()
    zmq_socket = context.socket(zmq.PULL)
    zmq_socket.set_hwm(args.batch_size)
    zmq_socket.bind('tcp://0.0.0.0:{port}'.format(port=args.zmq_port+1))
    logging.info('tester started (port: {port})'.format(port=args.zmq_port+1))

    args.output.write('_id,category_id\n')  # header

    __t0 = time.time()
    batch_data = np.zeros(batch_shape)
    batch_ids, batch_raw = [], []
    term_count = 0
    product_count = 0

    finished = False
    while not finished:
        images = zmq_socket.recv_pyobj()
        if images is None:
            term_count += 1
            if term_count == args.num_procs:
                finished = True
            continue

        pad_forward = False
        if len(images) + len(batch_ids) <= args.batch_size:
            for img, class_id, raw in images:
                batch_data[len(batch_ids)] = img
                batch_ids.append(class_id)
                batch_raw.append(raw)
            product_count += 1
        else:
            pad_forward = True

        if pad_forward or len(batch_ids) == args.batch_size:
            __t1 = time.time()
            probs_dict = _do_forward(tester, batch_data, batch_ids, batch_raw, md5_dict, cate2cid)
            __t2 = time.time()
            _write_output(args, probs_dict, cid2cate)
            __t3 = time.time()
            logging.info('[{4:8d}] batch:{0:.3f}, forward:{1:.3f}, write:{2:.3f} ({3:.3f}/s)'.format(
                __t1-__t0, __t2-__t1, __t3-__t2, args.batch_size / (__t3-__t0), product_count))

            batch_ids[:] = []
            batch_raw[:] = []

            if pad_forward and images:
                for img, class_id, raw in images:
                    batch_data[len(batch_ids)] = img
                    batch_ids.append(class_id)
                    batch_raw.append(raw)
                product_count += 1
            __t0 = time.time()

    __t1 = time.time()
    probs_dict = _do_forward(tester, batch_data, batch_ids, batch_raw, md5_dict, cate2cid)
    __t2 = time.time()
    _write_output(args, probs_dict, cid2cate)
    __t3 = time.time()
    logging.info('[{4:8d}] batch:{0:.3f}, forward:{1:.3f}, write:{2:.3f} ({3:.3f}/s)'.format(
        __t1 - __t0, __t2 - __t1, __t3 - __t2, args.batch_size / (__t3 - __t0), product_count))
    __t0 = time.time()

    logging.info('tester finished (product_count:{})'.format(product_count))


def _hwc_to_chw(img):
    img = np.swapaxes(img, 0, 2)
    img = np.swapaxes(img, 1, 2)
    return img


def _processor(args):
    context = zmq.Context()
    zmq_socket = context.socket(zmq.PULL)
    zmq_socket.set_hwm(0)
    zmq_socket.connect('tcp://0.0.0.0:{port}'.format(port=args.zmq_port))
    logging.info('processor started')

    ext_socket = context.socket(zmq.PUSH)
    ext_socket.set_hwm(args.batch_size)
    ext_socket.connect('tcp://0.0.0.0:{port}'.format(port=args.zmq_port+1))

    while True:
        items = zmq_socket.recv_pyobj()
        if items is None:
            logging.info('processor finished')
            ext_socket.send_pyobj(None)
            return
        images = []
        for _, img_bytes, class_id in items:
            img = cv2.imdecode(np.fromstring(img_bytes, np.uint8), cv2.IMREAD_COLOR)
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            images.append((_hwc_to_chw(img), class_id, img_bytes))
            if args.multi_view >= 1:
                img_flip = cv2.flip(img, flipCode=1)
                images.append((_hwc_to_chw(img_flip), class_id, img_bytes))
        ext_socket.send_pyobj(images)


def main(args):
    proc_reader = Process(target=_reader, args=(args,))
    proc_predict = Process(target=_predict, args=(args,))
    proc_processors = [Process(target=_processor, args=(args,)) for _ in range(args.num_procs)]

    proc_reader.start()
    proc_predict.start()
    [x.start() for x in proc_processors]

    proc_reader.join()
    proc_predict.join()
    [x.join() for x in proc_processors]


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--bson', type=str, required=True)
    parser.add_argument('--csv', type=str, required=True)
    parser.add_argument('--params', type=str, required=True)
    parser.add_argument('--symbol', type=str, default='3,180,180')
    parser.add_argument('--batch-size', type=int, required=True)
    parser.add_argument('--data-shape', type=str, required=True)
    parser.add_argument('--gpus', type=str, default='0')
    parser.add_argument('--num-procs', type=int, default=1)
    parser.add_argument('--zmq-port', type=int, default=18313)
    parser.add_argument('--md5-dict-pkl', type=str, default=None)

    parser.add_argument('--multi-view', type=int, default=0)

    parser.add_argument('--mean-max-pooling', action='store_true')
    parser.add_argument('--pool-name', type=str, default='pool1')
    parser.add_argument('--fc-name', type=str, default='fc')

    parser.add_argument('--output', type=argparse.FileType('w'), required=True)
    args = parser.parse_args()

    main(args)

