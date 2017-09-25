# -*- coding: utf-8 -*-

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

from __future__ import print_function
import os
import csv

import mxnet as mx
import argparse
import cv2
import random
import numpy as np
import time
import traceback
import bson

try:
    import multiprocessing
except ImportError:
    multiprocessing = None


def read_csv_category(csv_path):
    cate_dict = dict()
    with open(csv_path, 'r') as reader:
        csvreader = csv.reader(reader, delimiter=',', quotechar='"')
        for i, row in enumerate(csvreader):
            if i == 0:
                continue
            try:
                cateid, cate1, cate2, cate3 = row
                cate_dict[int(cateid)] = len(cate_dict)
            except Exception as e:
                print('cannot parse line: {}, {}'.format(row, e))
    print('read {} categories'.format(len(cate_dict)))
    return cate_dict


def read_images(bson_path, csv_path):
    cate_dict = read_csv_category(csv_path)

    data = bson.decode_file_iter(open(bson_path, 'rb'))

    idx = 0
    for c, d in enumerate(data):
        product_id = d.get('_id')
        category_id = d.get('category_id', None)  # This won't be in Test data
        for e, pic in enumerate(d['imgs']):
            picture = pic['picture']
            item = (idx, picture, cate_dict[category_id] if category_id else idx)
            idx += 1
            yield item  # id, picture, label, [label,]


def image_encode(args, i, item, q_out):
    if len(item) > 3 and args.pack_label:
        header = mx.recordio.IRHeader(0, item[2:], item[0], 0)
    else:
        header = mx.recordio.IRHeader(0, item[2], item[0], 0)

    if args.pass_through:
        try:
            s = mx.recordio.pack(header, item[1])
            q_out.put((i, s, item))
        except Exception as e:
            traceback.print_exc()
            print('pack_img error: {}, {}'.format(item[0], e))
            q_out.put((i, None, item))
        return

    try:
        nparr = np.fromstring(item[1], np.uint8)
        img = cv2.imdecode(nparr, args.color)
    except Exception as e:
        traceback.print_exc()
        print('imread error trying to load file: {}, {}'.format(item[0], e))
        q_out.put((i, None, item))
        return
    if img is None:
        print('imread read blank (None) image for file: {}'.format(item[0]))
        q_out.put((i, None, item))
        return
    if args.center_crop:
        if img.shape[0] > img.shape[1]:
            margin = (img.shape[0] - img.shape[1]) // 2
            img = img[margin:margin + img.shape[1], :]
        else:
            margin = (img.shape[1] - img.shape[0]) // 2
            img = img[:, margin:margin + img.shape[0]]
    if args.resize:
        if img.shape[0] > img.shape[1]:
            newsize = (args.resize, img.shape[0] * args.resize // img.shape[1])
        else:
            newsize = (img.shape[1] * args.resize // img.shape[0], args.resize)
        img = cv2.resize(img, newsize)

    try:
        s = mx.recordio.pack_img(header, img, quality=args.quality, img_fmt=args.encoding)
        q_out.put((i, s, item))
    except Exception as e:
        traceback.print_exc()
        print('pack_img error on file: {}, {}'.format(item[0], e))
        q_out.put((i, None, item))
        return


def read_worker(args, q_in, q_out):
    while True:
        deq = q_in.get()
        if deq is None:
            break
        i, item = deq
        image_encode(args, i, item, q_out)


def write_worker(q_out, args):
    random.seed(args.random_seed)

    pre_time = time.time()
    train_fname_rec = os.path.abspath(args.prefix + '_train.rec.tmp')
    train_fname_idx = os.path.abspath(args.prefix + '_train.idx.tmp')
    val_fname_rec = os.path.abspath(args.prefix + '_val.rec.tmp')
    val_fname_idx = os.path.abspath(args.prefix + '_val.idx.tmp')
    train_record = mx.recordio.MXIndexedRecordIO(train_fname_idx, train_fname_rec, 'w')
    val_record = mx.recordio.MXIndexedRecordIO(val_fname_idx, val_fname_rec, 'w')

    count, train_count, val_count = 0, 0, 0
    train_buf = list()
    buf = {}
    more = True
    start_time = time.time()
    while more:
        deq = q_out.get()
        if deq is not None:
            i, s, item = deq
            buf[i] = (s, item)
        else:
            more = False
        while count in buf:
            s, item = buf[count]
            del buf[count]
            if s is not None:
                train_buf.append((item[0], s))

            if len(train_buf) >= 1000000:
                if args.shuffle:
                    print('shuffling...')
                    random.shuffle(train_buf)
                for x in train_buf:
                    if random.random() < args.val_ratio:
                        val_record.write_idx(x[0], x[1])
                        val_count += 1
                    else:
                        train_record.write_idx(x[0], x[1])
                        train_count += 1
                train_buf = list()

            count += 1
            if count % 10000 == 0:
                cur_time = time.time()
                print('[{6:10d}] elapsed_time: {0:.3f}, step_time:{1:.3f}, train_count: {2}({3:.3f}), val_count: {4}({5:.3f})'.format(
                    cur_time - start_time, cur_time - pre_time, train_count, train_count / count, val_count, val_count / count, count
                ))
                pre_time = cur_time

    if args.shuffle:
        random.shuffle(train_buf)
    for x in train_buf:
        if random.random() < args.val_ratio:
            val_record.write_idx(x[0], x[1])
            val_count += 1
        else:
            train_record.write_idx(x[0], x[1])
            train_count += 1

    cur_time = time.time()
    print('[{6:10d}] elapsed_time: {0:.3f}, step_time:{1:.3f}, train_count: {2}({3:.3f}), val_count: {4}({5:.3f})'.format(
        cur_time - start_time, cur_time - pre_time, train_count, train_count / count, val_count, val_count / count, count
    ))


def parse_args():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description='Create an image list or \
        make a record database by reading from an image list')
    parser.add_argument('--prefix', type=str, required=True,
                        help='prefix of input/output lst and rec files.')
    parser.add_argument('--bson', type=str, required=True)
    parser.add_argument('--csv', type=str, default='/home/deploy/dylan/dataset/cdiscount/category_names.csv')
    parser.add_argument('--random-seed', type=int, default=0xC0FFEE)
    parser.add_argument('--val-ratio', type=float, default=0)
    parser.add_argument('--shuffle', action='store_true')

    rgroup = parser.add_argument_group('Options for creating database')
    rgroup.add_argument('--pass-through', action='store_true',
                        help='whether to skip transformation and save image as is')
    rgroup.add_argument('--resize', type=int, default=0,
                        help='resize the shorter edge of image to the newsize, original images will\
        be packed by default.')
    rgroup.add_argument('--center-crop', type=bool, default=False,
                        help='specify whether to crop the center image to make it rectangular.')
    rgroup.add_argument('--quality', type=int, default=95,
                        help='JPEG quality for encoding, 1-100; or PNG compression for encoding, 1-9')
    rgroup.add_argument('--num-thread', type=int, default=1,
                        help='number of thread to use for encoding. order of images will be different\
        from the input list if >1. the input list will be modified to match the\
        resulting order.')
    rgroup.add_argument('--color', type=int, default=1, choices=[-1, 0, 1],
                        help='specify the color mode of the loaded image.\
        1: Loads a color image. Any transparency of image will be neglected. It is the default flag.\
        0: Loads image in grayscale mode.\
        -1:Loads image as such including alpha channel.')
    rgroup.add_argument('--encoding', type=str, default='.jpg', choices=['.jpg', '.png'],
                        help='specify the encoding of the images.')
    rgroup.add_argument('--pack-label', type=bool, default=False,
                        help='Whether to also pack multi dimensional label in the record file')
    args = parser.parse_args()
    args.prefix = os.path.abspath(args.prefix)
    print(args)
    return args


if __name__ == '__main__':
    args = parse_args()

    if os.path.isdir(args.prefix):
        raise ValueError('args.prefix is not file prefix')

    count = 0

    print('Creating .rec file from', args.bson)
    count += 1
    image_list = read_images(args.bson, args.csv)
    # -- write_record -- #
    q_in = [multiprocessing.Queue(1024) for i in range(args.num_thread)]
    q_out = multiprocessing.Queue(1024)
    read_process = [multiprocessing.Process(target=read_worker,
                                            args=(args, q_in[i], q_out))
                    for i in range(args.num_thread)]

    for p in read_process:
        p.start()
    write_process = multiprocessing.Process(target=write_worker, args=(q_out, args))
    write_process.start()

    for i, item in enumerate(image_list):
        q_in[i % len(q_in)].put((i, item))
    for q in q_in:
        q.put(None)
    for p in read_process:
        p.join()

    q_out.put(None)
    write_process.join()

    if not count:
        print('Did not find and list file with prefix %s' % args.prefix)
