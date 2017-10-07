#!/usr/bin/env bash

python3 -u bson2rec_simple.py \
    --bson          /home/deploy/dylan/projects/kaggle-cdiscount/data/refined_val.bson \
    --category-csv  /home/deploy/dylan/dataset/cdiscount/category_names.csv \
    --out-rec       /home/deploy/dylan/projects/kaggle-cdiscount/data/refined_val.rec \
    --unique-md5
