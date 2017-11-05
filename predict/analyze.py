# -*- coding: utf-8 -*-

import sys
import os
import csv
from collections import defaultdict, Counter

import bson
from tqdm import tqdm

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data.category import get_category_dict


def main(args):
    cate1_dict, cate2_dict, cate3_dict = get_category_dict()

    products = dict()
    with open(args.bson_path, 'rb') as reader:
        data = bson.decode_file_iter(reader)
        for c, d in tqdm(enumerate(data), unit='products'):
            product_id = d.get('_id')
            category_id = d.get('category_id', None)  # This won't be in Test data
            products[product_id] = {
                'product_id': product_id,
                'category_id': category_id,
                'images': [pic.get('picture') for e, pic in enumerate(d['imgs'])],
            }

    cate1_total_counter, cate1_correct_counter = Counter(), Counter()
    with open(args.predict_csv, 'r') as reader:
        csvreader = csv.reader(reader, delimiter=',', quotechar='"')
        for i, row in enumerate(csvreader):
            if i == 0:  # ignore header line
                continue
            prod_id, cate_id = (int(x) for x in row)
            cate1, cate2, cate3 = cate3_dict[cate_id]['names']
            cate1_total_counter[cate1] += 1
            if products[prod_id]['category_id'] == cate_id:
                cate1_correct_counter[cate1] += 1
            else:
                pass

    print('Accuracy: {:.6f}'.format(sum(cate1_correct_counter.values())/sum(cate1_total_counter.values())))
    for cate1 in cate1_total_counter:
        cate1_accuracy = cate1_correct_counter[cate1] / cate1_total_counter[cate1]
        print('{:8d}\t{:8d}\t{:8d}\t{:.6f}\t[{:02d}] {}'.format(cate1_total_counter[cate1],
                                                                cate1_correct_counter[cate1],
                                                                cate1_total_counter[cate1] - cate1_correct_counter[cate1],
                                                                cate1_accuracy,
                                                                cate1_dict[(cate1,)]['cate1_class_id'], cate1))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--predict-csv', type=str, required=True)
    parser.add_argument('--bson-path', type=str, required=True)
    parser.add_argument('--category-csv', type=str, required=True)
    args = parser.parse_args()

    main(args)

