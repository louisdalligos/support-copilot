"""Retrieval over the store's policy and help documents.

The pipeline: split documents into section chunks, search them two ways
(BM25 keywords and dense embeddings), merge the two rankings with reciprocal
rank fusion, then rerank the top candidates with a cross-encoder. Each stage
is optional, so the evals can compare them and CI can run the keyword-only
pipeline without downloading models.
"""
