# Author: Nadja Rhodes
# License: BSD 3-Clause
# Modified from Kyle Kastner's example here:
# https://github.com/kastnerkyle/pytorch-text-vae
import time
import os
try:
    import Queue
except ImportError:
    import queue as Queue
import multiprocessing as mp
import dill as pickle
from enum import Enum

import numpy as np
import re
import sys
import unidecode
import unicodedata
import collections
import pandas as pd

SOS_token = 0
EOS_token = 1
UNK_token = 2
N_CORE = 24

class Condition(Enum):
    NONE = 0
    GENRE = 1
    AF = 2 # audio features

class DataSplit:
    def __init__(self, filename, data_type):
        self.filename = filename
        self.data_type = data_type

        self.df = pd.read_json(self.filename)
        self.n_conditions = -1

    def __iter__(self):
        if self.data_type == Dataset.DataType.JSON:
            return self.read_json_gen()
        else:
            return self.read_file_line_gen()

    def read_file_line_gen(self):
        with open(self.filename) as f:
            for line in f:
                yield unidecode.unidecode(line)

    def encode_conditions(self, conditions):
        raise NotImplementedError

    def decode_conditions(self, tensor):
        raise NotImplementedError

    def read_json_gen(self):
        for i, row in self.df.iterrows():
            for sent in row.content_sentences:
                yield sent

class GenreDataSplit(DataSplit):
    def __init__(self, filename, data_type, condition_set=None):
        super(GenreDataSplit, self).__init__(filename, data_type)

        if condition_set:
            self.condition_set = condition_set
        else:
            self.condition_set = set([g for gg in self.df.spotify_genres for g in gg])

        self.genre_to_idx = {unique_g: i for i, unique_g in enumerate(sorted(self.condition_set))}
        self.idx_to_genre = {i: unique_g for i, unique_g in enumerate(sorted(self.condition_set))}

        self.n_conditions = len(self.condition_set) + 1

    def encode_conditions(self, conditions):
        e = np.zeros(self.n_conditions)
        for g in conditions:
            if g in self.genre_to_idx:
                e[self.genre_to_idx[g]] = 1
            else:
                # for unknown genres
                e[len(e) - 1] = 1
        return e

    def decode_conditions(self, tensor):
        genres = []
        for i, x in enumerate(tensor.squeeze()):
            if x.item() == 1:
                if i in self.idx_to_genre:
                    genres.append(self.idx_to_genre[i])
                else:
                    genres.append('UNK')
        return genres

    def read_json_gen(self):
        for i, row in self.df.iterrows():
            gs = self.encode_conditions(row.spotify_genres)
            for sent in row.content_sentences:
                yield sent, gs

class AFDataSplit(DataSplit):
    def __init__(self, filename, data_type):
        super(AFDataSplit, self).__init__(filename, data_type)

        import json
        # all rows should have the same condition keys
        self.ignore_keys = ['analysis_url', 'duration_ms', 'id', 'track_href', 'type', 'uri']
        condition_list = [k for (k, v) in sorted(json.loads(df.audio_features[0].replace("'", "\"")).items()) if k not in self.ignore_keys]

        self.n_conditions = len(condition_list)
        self.idx_to_af = {i: c for i, c in enumerate(condition_list)}

    def encode_conditions(self, conditions):
        return np.array([v for (k, v) in sorted(conditions.items()) if k not in self.ignore_keys])

    def decode_conditions(self, tensor):
        afs = {}
        for i, x in enumerate(tensor.squeeze()):
            afs[self.idx_to_af[i]] = x.item()
        return afs

    def read_json_gen(self):
        for i, row in self.df.iterrows():
            try:
                fs = self.encode_conditions(json.loads(row.audio_features.replace("'", "\"")))
            except json.decoder.JSONDecodeError:
                # TODO: why audio_features = None ever?
                fs = np.zeros(self.n_conditions)
            for sent in row.content_sentences:
                yield sent, fs

class Dataset:
    class DataType(Enum):
        DEFAULT = 0
        JSON = 1

    def __init__(self, trn_path, test_path=None):
        if trn_path.endswith('.json'):
            self.data_type = Dataset.DataType.JSON
        else:
            self.data_type = Dataset.DataType.DEFAULT

        self.trn_split = DataSplit(trn_path, self.data_type)
        if test_path:
            self.test_split = DataSplit(test_path, self.data_type)
        else:
            self.test_split = None

class GenreDataset(Dataset):
    def __init__(self, trn_path, test_path=None):
        super(GenreDataset, self).__init__(trn_path, test_path)

        self.trn_split = GenreDataSplit(trn_path, self.data_type)
        if test_path:
            self.test_split = GenreDataSplit(test_path, self.data_type, self.trn_split.condition_set)
        else:
            self.test_split = None

class AFDataset(Dataset):
    def __init__(self, trn_path, test_path=None):
        super(AFDataset, self).__init__(trn_path, test_path)

        self.trn_split = AFDataSplit(trn_path, self.data_type)
        if test_path:
            self.test_split = AFDataSplit(test_path, self.data_type)
        else:
            self.test_split = None

    def get_mean_condition(self, pairs):
        if not hasattr(self, 'mean_condition'):
            conditions = np.array([p[2] for p in pairs])
            self.mean_condition = np.mean(conditions, axis=0)

        return self.mean_condition


norvig_list = None
# http://norvig.com/ngrams/count_1w.txt
# TODO: replace with spacy tokenization? or is it better to stick to common words?
'''Things turned to UNK:
- numbers
'''
def get_vocabulary(tmp_path):
    global norvig_list
    global reverse_norvig_list
    if norvig_list == None:
        with open(os.path.join(tmp_path, "count_1w.txt")) as f:
            r = f.readlines()
        norvig_list = [tuple(ri.strip().split("\t")) for ri in r]
    return norvig_list


# Turn a Unicode string to plain ASCII, thanks to http://stackoverflow.com/a/518232/2809427
def unicode_to_ascii(s):
    return ''.join(
        c for c in unicodedata.normalize(u'NFD', s)
        if unicodedata.category(c) != u'Mn'
    )


# Lowercase, trim, and remove non-letter characters
def normalize_string(s):
    s = unicode_to_ascii(s.lower().strip())
    s = re.sub(r"'", r"", s)
    s = re.sub(r"([.!?])", r" \1", s)
    #s = re.sub(r"[^a-zA-Z.!?]+", r" ", s)
    s = re.sub(r"[^\w]", r" ", s)
    s = re.sub(r"\s+", r" ", s).strip().lstrip().rstrip()
    return s


class Lang:
    def __init__(self, name, tmp_path, vocabulary_size=-1, reverse=False):
        self.name = name
        if reverse:
            self.vocabulary = [w[::-1] for w in ["SOS", "EOS", "UNK"]] + [w[0][::-1] for w in get_vocabulary(tmp_path)]
        else:
            self.vocabulary = ["SOS", "EOS", "UNK"] + [w[0] for w in get_vocabulary(tmp_path)]

        if vocabulary_size < 0:
            vocabulary_size = len(self.vocabulary)

        self.reverse = reverse
        self.vocabulary_size = vocabulary_size
        if vocabulary_size < len(self.vocabulary):
            print(f"Trimming vocabulary size from {len(self.vocabulary)} to {vocabulary_size}")
        else:
            print(f"Vocabulary size: {vocabulary_size}")
        self.vocabulary = self.vocabulary[:vocabulary_size]
        self.word2index = {v: k for k, v in enumerate(self.vocabulary)}
        self.index2word = {v: k for k, v in self.word2index.items()}
        self.n_words = len(self.vocabulary) # Count SOS, EOS, UNK
        # dict.keys() do not pickle in Python 3.x - convert to list
        # https://groups.google.com/d/msg/pyomo-forum/XOf6zwvEbt4/ZfkbHzvDBgAJ
        self.words = list(self.word2index.keys())
        self.indices = list(self.index2word.keys())

    def index_to_word(self, index):
        try:
            return self.index2word[index.item()]
        except KeyError:
            return self.index2word[self.word2index[self.vocabulary[UNK_token]]]

    def word_to_index(self, word):
        try:
            return self.word2index[word.lower()]
        except KeyError:
            #print(f"[WARNING] {word.lower()}")
            return self.word2index[self.vocabulary[UNK_token]]

    def word_check(self, word):
        if word in self.word2index.keys():
            return word
        else:
            return self.word2index[self.vocabulary[UNK_token]]

    def process_sentence(self, sentence, normalize=True):
        if normalize:
            s = normalize_string(sentence)
        else:
            s = sentence
        return " ".join([w if w in self.words else self.word2index[self.vocabulary[UNK_token]] for w in s.split(" ")])

def filter_pair(p):
    return MIN_LENGTH < len(p[0].split(' ')) < MAX_LENGTH and MIN_LENGTH < len(p[1].split(' ')) < MAX_LENGTH


def process_input_side(s):
    return " ".join([WORDS[w] for w in s.split(" ")])


def process_output_side(s):
    return " ".join([REVERSE_WORDS[w] for w in s.split(" ")])


WORDS = None
REVERSE_WORDS = None

def unk_func():
    return "UNK"

def _get_line(data_type, elem):
    # JSON data can come with extra conditional info
    if data_type == Dataset.DataType.JSON and not isinstance(elem, str):
        line = elem[0]
    else:
        line = elem

    return line

def _setup_vocab(trn_path, vocabulary_size, condition_on):
    global WORDS
    global REVERSE_WORDS
    wc = collections.Counter()
    if condition_on == Condition.GENRE:
        dataset = GenreDataset(trn_path)
    elif condition_on == Condition.AF:
        dataset = AFDataset(trn_path)
    else:
        dataset = Dataset(trn_path)
    for n, elem in enumerate(iter(dataset.trn_split)):
        if n % 100000 == 0:
            print("Fetching vocabulary from line {}".format(n))
            print("Current word count {}".format(len(wc.keys())))

        line = _get_line(dataset.data_type, elem)

        l = line.strip().lstrip().rstrip()
        if MIN_LENGTH < len(l.split(' ')) < MAX_LENGTH:
            l = normalize_string(l)
            WORDS = l.split(" ")
            wc.update(WORDS)
        else:
            continue

    the_words = ["SOS", "EOS", "UNK"]
    the_reverse_words = [w[::-1] for w in the_words]
    the_words += [wi[0] for wi in wc.most_common()[:vocabulary_size - 3]]
    the_reverse_words += [wi[0][::-1] for wi in wc.most_common()[:vocabulary_size - 3]]

    WORDS = collections.defaultdict(unk_func)
    REVERSE_WORDS = collections.defaultdict(unk_func)
    for k in range(len(the_words)):
        WORDS[the_words[k]] = the_words[k]
        REVERSE_WORDS[the_reverse_words[k]] = the_reverse_words[k]


def proc_line(line, reverse):
    if len(line.strip()) == 0:
        return None
    else:
        l = line.strip().lstrip().rstrip()
        # try to bail as early as possible to minimize processing
        if MIN_LENGTH < len(l.split(' ')) < MAX_LENGTH:
            l = normalize_string(l)
            l2 = l
            pair = (l, l2)

            if filter_pair(pair):
                if reverse:
                    pair = (l, "".join(list(reversed(l2))))
                p0 = process_input_side(pair[0])
                p1 = process_output_side(pair[1])
                return (p0, p1)
            else:
                return None
        else:
            return None


def process(q, oq, iolock):
    while True:
        stuff = q.get()
        if stuff is None:
            break
        r = [(proc_line(s[0], True), s[1]) if isinstance(s, tuple) else proc_line(s, True) for s in stuff]
        r = [ri for ri in r if ri != None and ri[0] != None]
        # flatten any tuples
        r = [ri[0] + (ri[1], ) if isinstance(ri[0], tuple) else ri for ri in r]
        if len(r) > 0:
            oq.put(r)

def _setup_pairs(datasplit):
    print("Setting up queues")
    # some nasty multiprocessing
    # ~ 40 per second was the single core number
    q = mp.Queue(maxsize=1000000 * N_CORE)
    oq = mp.Queue(maxsize=1000000 * N_CORE)
    print("Queue setup complete")
    print("Getting lock")
    iolock = mp.Lock()
    print("Setting up pool")
    pool = mp.Pool(N_CORE, initializer=process, initargs=(q, oq, iolock))
    print("Pool setup complete")

    start_time = time.time()
    pairs = []
    last_empty = time.time()

    curr_block = []
    block_size = 1000
    last_send = 0
    # takes ~ 30s to get a block done
    empty_wait = 2
    avg_time_per_block = 30
    status_every = 100000
    print("Starting block processing")

    for n, elem in enumerate(iter(datasplit)):
        curr_block.append(elem)
        if len(curr_block) > block_size:
            # this could block, oy
            q.put(curr_block)
            curr_block = []

        if last_empty < time.time() - empty_wait:
            try:
                while True:
                    with iolock:
                        r = oq.get(block=True, timeout=.0001)
                    pairs.extend(r)
            except:
                last_empty = time.time()
        if n % status_every == 0:
            with iolock:
                print("Queued line {}".format(n))
                tt = time.time() - start_time
                print("Elapsed time {}".format(tt))
                tl = len(pairs)
                print("Total lines {}".format(tl))
                avg_time_per_block = max(30, block_size * (tt / (tl + 1)))
                print("Approximate lines / s {}".format(tl / tt))
    # finish the queue
    q.put(curr_block)
    print("Finalizing line processing")
    for _ in range(N_CORE):  # tell workers we're done
        q.put(None)
    empty_checks = 0
    prev_len = len(pairs)
    last_status = time.time()
    print("Total lines {}".format(len(pairs)))
    while True:
        if empty_checks > 10:
            break
        if status_every < (len(pairs) - prev_len) or last_status < time.time() - empty_wait:
            print("Total lines {}".format(len(pairs)))
            prev_len = len(pairs)
            last_status = time.time()
        if not oq.empty():
            try:
                while True:
                    with iolock:
                        r = oq.get(block=True, timeout=.0001)
                    pairs.extend(r)
                    empty_checks = 0
            except:
                # Queue.Empty
                pass
        elif oq.empty():
            empty_checks += 1
            time.sleep(empty_wait)
    print("Line processing complete")
    print("Final line count {}".format(len(pairs)))
    pool.close()
    pool.join()

    return pairs

# https://stackoverflow.com/questions/43078980/python-multiprocessing-with-generator
def prepare_pair_data(path, vocabulary_size, tmp_path, min_length, max_length, condition_on, reverse=False):
    global MIN_LENGTH
    global MAX_LENGTH
    MIN_LENGTH, MAX_LENGTH = min_length, max_length

    print("Reading lines...")
    print(f'MIN_LENGTH: {MIN_LENGTH}; MAX_LENGTH: {MAX_LENGTH}')

    if os.path.isdir(path):
        # assume folder contains separate train.json and test.json
        # TODO: would be cool not to assume .json format
        trn_path = os.path.join(path, 'train.json')
        test_path = os.path.join(path, 'test.json')
    else:
        trn_path = path
        test_path = None

    pkl_path = trn_path.split(os.sep)[-1].split(".")[0] + "_vocabulary.pkl"
    vocab_cache_path = os.path.join(tmp_path, pkl_path)
    global WORDS
    global REVERSE_WORDS
    if not os.path.exists(vocab_cache_path):
        print("Vocabulary cache {} not found".format(vocab_cache_path))
        print("Prepping vocabulary")
        _setup_vocab(trn_path, vocabulary_size, condition_on)
        with open(vocab_cache_path, "wb") as f:
            pickle.dump((WORDS, REVERSE_WORDS), f)
    else:
        print("Vocabulary cache {} found".format(vocab_cache_path))
        print("Loading...".format(vocab_cache_path))
        with open(vocab_cache_path, "rb") as f:
            r = pickle.load(f)
        WORDS = r[0]
        REVERSE_WORDS = r[1]
    print("Vocabulary prep complete")

    if condition_on == Condition.GENRE:
        dataset = GenreDataset(trn_path, test_path)
    elif condition_on == Condition.AF:
        dataset = AFDataset(trn_path, test_path)
    else:
        dataset = Dataset(trn_path, test_path)

    # don't use these for processing, but pass for ease of use later on
    dataset.input_side = Lang("in", tmp_path, vocabulary_size)
    dataset.output_side = Lang("out", tmp_path, vocabulary_size, reverse)

    print("Pair preparation for train split")
    dataset.trn_pairs = _setup_pairs(dataset.trn_split)

    if dataset.test_split:
        print("Pair preparation for test split")
        dataset.test_pairs = _setup_pairs(dataset.test_split)
    else:
        dataset.test_pairs = None

    print("Pair preparation complete")
    return dataset
