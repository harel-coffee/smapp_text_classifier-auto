'''
Author: Fridolin Linder
'''
import os
import logging
import warnings
import numpy as np
import pandas as pd
import gensim
import joblib
import hashlib

from gensim.models import FastText
from sklearn.feature_extraction.text import CountVectorizer
from sklearn.base import TransformerMixin, BaseEstimator
from smapp_text_classifier.utilities import timeit

class CacheError(ValueError):
    '''Basic exception for errors related to the vectorizer cache'''
    def __init__(self, msg=None):
        msg = "Cache not found" if msg is None else msg
        super().__init__(msg)

def hash_document(document):
    encoded = document.encode('utf-8')
    return hashlib.md5(encoded).hexdigest()

@timeit
def hash_corpus(documents):
    '''store a index-md5 hash mapping for each document to be able to 
    check the docs later'''
    out = pd.DataFrame(
        {'md5': [hash_document(s) for s in documents]}
    )
    out.index = documents.index
    return out

class CachedVectorizer:
    '''Base class to build cached vectorizer.

    Attributes:
        recompute: bool, if True the cache is ignored, the vectorizer is fitted
            and the feature matrix recomputed
        cache: str, path to the file to cache the vectorizer + feature matrix
    '''
    def __init__(self, cache_dir='./', recompute=False):
        self.cache_dir = cache_dir
        self.recompute = recompute
        self.feature_matrix = None
        self.index_mapping = None
        self.doc_md5 = None
        if not os.path.exists(self.cache_dir):
            os.mkdir(self.cache_dir)

    @timeit
    def _load_from_cache(self, cache):
        if self.recompute:
            logging.debug('Not loading due to recompute request')
            raise CacheError()
        elif os.path.exists(cache):
            cached_self = joblib.load(cache)
            del cached_self.recompute
            self.__dict__.update(cached_self.__dict__)
        else:
            logging.debug('Cache not found')
            raise CacheError()

    def get_docs(self, raw_idxs):
        try:
            mapped = [self.index_mapping[idx] for idx in raw_idxs]
        except KeyError:
            logging.debug('Index of passed data does not match cached data')
            raise CacheError()
        return self.feature_matrix[mapped, ]
    
   
    def _check_X(self, X):
        '''Check if document input has an index and if raw_docs match 
        the cached data'''
        # Check the input documents have an index
        if not hasattr(X, 'index'):
            raise ValueError('X needs index if vectorized from cache')
        # Check if all requested index elements are in the cache index
        if not all(X.index.isin(self.doc_md5.index)):
            logging.debug('Not all indices of new documents are in cache index.')
            raise CacheError()
        # If the vectorizer has been fitted (i.e. has a cached feature matrix) 
        # check sample of 5 documents to make sure the docs match the ones in the
        # cached data. If not raise a CacheError and print appropriate warning
        if self.feature_matrix is not None:
            logging.debug('Checking if cache matches index docs')
            sample_idxs = np.random.choice(X.index, size=5, replace=False)
            for idx in sample_idxs:
                d_hash = hash_document(X.loc[idx])
                if self.doc_md5['md5'].loc[idx] != d_hash:
                    logging.warning('Indices overlapping but mismatch of '
                                    'documents and cache.')
                    raise CacheError()
                #sample_trans = self.transform_from_scratch([X.loc[idx]])
                #nz_sample = sample_trans.nonzero()[1]
                #sample_cache = self.get_docs([idx]).nonzero()[1]
                #if set(nz_sample) != set(sample_cache):
                #    logging.warning(
                #        'Mismatch of documents and cache. Falling back to'
                #        ' transforming from scratch'
                #    )
                #    raise CacheError()

    def transform_from_scratch(self, X):
        raise NotImplementedError('This method must be overwritten')


class CachedCountVectorizer(CountVectorizer, CachedVectorizer):
    '''Modification of the sklearn CountVectorizer to allow caching

    The vectorizer dumps itself + the document - term matrix for the set of
    documents it was fitted to to a cache file. When re-used, subsets of the
    initial document can be vectorized by pulling the corresponding rows from
    the full document-term matrix. This avoids re-tokenization and vectorization

    There are multiple peculiar design choices (e.g. cache as a property) that
    stem from the fact that this class is meant to be used in a sklearn pipeline
    that is cross validated using the sklearn cross-validation tools. I.e.
    during cross validation, the base estimator is cloned (that is, the
    estimator with default paramter settings) and the parameters that are tuned
    are set dynamically after initialization.

    Attributes:
        cache_dir: str, directory to cache the vectorizer to
        ds_name: str, name of the dataset the vectorizer is fitted to
        recompute: bool, ignore the cache if True
        ngram_range: tuple, see parent documentation
        analyzer: str, see parent documentation
        **kwargs: additional arguments passed to CountVectorizer, see parent
            documentation
    '''
    def __init__(self, cache_dir='./', ds_name='test', ngram_range=(1, 1),
                 analyzer='word', recompute=False, **kwargs):
        super().__init__(ngram_range=ngram_range, analyzer=analyzer, **kwargs)
        self.ds_name = ds_name
        super(CountVectorizer, self).__init__(cache_dir, recompute)


    @property
    def cache(self):
        return os.path.join(
            self.cache_dir,
            f'{self.ds_name}_{self.analyzer}_{self.ngram_range}.joblib'
        )

    @timeit
    def transform(self, raw_documents):
        try:
            logging.debug('Transforming from cache')
            self._load_from_cache(self.cache)
            self._check_X(raw_documents)
            return self.get_docs(raw_documents.index)
        except CacheError:
            logging.debug('Transforming from scratch')
            self.transform_from_scratch(raw_documents)

    @timeit
    def fit_transform(self, raw_documents, y=None):
        try:
            logging.debug('Transforming from cache')
            self._load_from_cache(self.cache)
            self._check_X(raw_documents)
            return self.get_docs(raw_documents.index)

        except CacheError:
            logging.debug('Transforming from scratch')
            self.feature_matrix = super().fit_transform(raw_documents)
            self.doc_md5 = hash_corpus(raw_documents)
            # keep track of what index location of input maps to which row in
            # the feature matrix
            self.index_mapping = {
                    idx: i for i, idx in enumerate(raw_documents.index)
            }
            ## Store md5 sum of each doc to easily check later
            #self.doc_md5 = hash_corpus(raw_documents)
            joblib.dump(self, self.cache)
            return self.feature_matrix
    
    def transform_from_scratch(self, X):
        return super().transform(X)



class CachedEmbeddingVectorizer(TransformerMixin, BaseEstimator,
                                CachedVectorizer):
    '''Vectorizer to transform a set of text documents into a matrix of document
    embeddings.

    The vectorizer dumps itself + the document - term matrix for the set of
    documents it was fitted to to a cache file. When re-used, subsets of the
    initial document can be vectorized by pulling the corresponding rows from
    the full document-term matrix. This avoids re-tokenization and vectorization

    There are multiple peculiar design choices (e.g. cache as a property) that
    stem from the fact that this class is meant to be used in a sklearn pipeline
    that is cross validated using the sklearn cross-validation tools. I.e.
    during cross validation, the base estimator is cloned (that is, the
    estimator with default paramter settings) and the parameters that are tuned
    are set dynamically after initialization.

    Attributes:
        embedding_model: tuple(name, path), identifier and path to the embedding
            model. Currently only Fasttext embeddings in Gensim or Facebook
            binary Fasttext format are supported.
        cache_dir: str, directory to cache the vectorizer to.
        ds_name: str, name of the dataset the vectorizer is fitted to.
        pooling_method: str, one of [`mean`, `max`]. Method to combine word
            vectors to a document vector.
        recompute: bool, ignore the cache if True
        tokenizer: function that tokenizes a string to a list of tokens
    '''
    def __init__(self, embedding_model, cache_dir=None,
                 ds_name=None, pooling_method=None,
                 tokenizer=None, recompute=False):
        self.embedding_model = embedding_model
        self.pooling_method = pooling_method
        self.ds_name = ds_name
        self.dimensionality = None
        super(BaseEstimator, self).__init__(cache_dir, recompute)
        self.tokenize = tokenizer

    @property
    def cache(self):
        return os.path.join(
            self.cache_dir,
            (f'{self.ds_name}_{self.embedding_model[0]}_'
             f'{self.pooling_method}.pkl')
        )

    def fit(self, X):
        self.fit_transform(X)
        return self

    def fit_transform(self, X, y=None):
        return self.transform(X)

    @timeit
    def transform(self, X, y=None):
        try:
            logging.debug('Transforming from cache')
            self._load_from_cache(self.cache)
            self._check_X(X)
            return self.get_docs(X.index)
        except CacheError:
            logging.debug('Transforming from scratch')
            em = self._load_embedding_model()
            logging.debug('Embedding documents')
            self.feature_matrix = np.array([self._embed_doc(doc, em)
                                            for doc in X])
            # keep track of what index location of input maps to which row in
            # the feature matrix
            self.index_mapping = {
                    idx: i for i, idx in enumerate(X.index)
            }
            ## Store md5 sum of each doc to easily check later
            self.doc_md5 = hash_corpus(X)
            joblib.dump(self, self.cache)
            return self.feature_matrix

    def transform_from_scratch(self, X):
        return self.transform(X)

    @timeit
    def _load_embedding_model(self):
        # Quick hack to allow passing of pre-loaded models
        # TODO: change naming of attribute and update documentation
        #       this might be a useful feature
        logging.debug('Loading embedding model')
        if isinstance(self.embedding_model[1],
                      gensim.models.fasttext.FastText):
            em = self.embedding_model[1]
        else:
            if self.embedding_model[1].endswith('.model'):
                em = FastText.load(self.embedding_model[1])
            else:
                em = FastText.load_fasttext_format(self.embedding_model[1])
        self.dimensionality = em.wv[em.wv.index2word[0]].shape[0]
        return em

    def _get_vector(self, word, model):
        '''Method to retrieve word vector from gensim model that is save
        for words that are not in the vocabulary (for fasttext: words of
        which no character n-gram is in the vocabulary; should be rare)
        '''
        try:
            return model[word]
        except KeyError:
            return np.zeros(self.dimensionality)

    def _embed_doc(self, doc, model):
        '''Calculate the document embedding by pooling the embedding of each
        token in the document
        '''
        tokens = self.tokenize(doc)
        if len(tokens) == 0:
            return np.zeros(self.dimensionality)
        vectors = np.array([self._get_vector(t, model.wv) for t in tokens])
        if self.pooling_method == 'mean':
            doc_vector = vectors.mean(axis=0)
        elif self.pooling_method == 'max':
            doc_vector = vectors.max(axis=0)
        else:
            raise ValueError(f'Unsupported pooling method: '
                             f'{self.pooling_method}')
        return doc_vector
