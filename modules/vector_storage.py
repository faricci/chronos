# modules/vector_storage.py
import faiss
import numpy as np
import os
import pickle

"""
FaissVectorStore manages a FAISS index for vector storage and similarity search.

Attributes:
    index_path (str): Path to the FAISS index file.
    embedding_dim (int): Dimensionality of the vectors to be stored.
    index (faiss.IndexFlatL2): FAISS index for storing vectors.
    vectors (list): List of stored vectors (in Python).
    metadata (list): List of metadata corresponding to the stored vectors.

Methods:
    add_vectors(vectors, metadata):
        Adds a list of vectors and their metadata to the FAISS index.
    similarity_search(query_vector, k=5):
        Performs a similarity search and returns the top k results (distance, metadata).
    save_index():
        Serializes the FAISS index to self.index_path.
"""

class FaissVectorStore:
    def __init__(self, index_path="data/faiss_index.index", embedding_dim=384):
        self.index_path = index_path
        self.embedding_dim = embedding_dim

        self.vectors = []
        self.metadata = []

        if os.path.exists(index_path):
            with open(index_path, "rb") as f:
                data = pickle.load(f)
            # 'data' is a dict with keys: 'faiss_index', 'vectors', 'metadata'
            loaded_index = data["faiss_index"]
            loaded_vectors = data["vectors"]
            loaded_metadata = data["metadata"]

            # Optional dimension check
            if loaded_index.d == embedding_dim:
                self.index = loaded_index
                self.vectors = loaded_vectors
                self.metadata = loaded_metadata
            else:
                print(f"WARNING: Loaded index dimension ({loaded_index.d}) != {embedding_dim}. Creating new index.")
                self.index = faiss.IndexFlatL2(embedding_dim)
        else:
            self.index = faiss.IndexFlatL2(embedding_dim)

    def add_vectors(self, vectors, metadata):
        """
        vectors: list of np.array, shape = (N, embedding_dim)
        metadata: list of dict (or any identifier) corresponding to each vector
        """
        if not vectors:
            return
        vecs = np.vstack(vectors).astype(np.float32)
        self.index.add(vecs)
        self.vectors.extend(vectors)
        self.metadata.extend(metadata)

    def similarity_search(self, query_vector, k=5):
        """
        query_vector: np.array (embedding_dim,)
        returns: list of (distance, metadata) pairs
        """
        if query_vector is None or len(query_vector) != self.embedding_dim:
            raise ValueError(f"Query vector must be of shape ({self.embedding_dim},).")
        query_vector = query_vector.astype(np.float32).reshape(1, -1)

        distances, indices = self.index.search(query_vector, k)
        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if 0 <= idx < len(self.metadata):
                results.append((dist, self.metadata[idx]))
        return results

    def save_index(self):
        """
        Serialize the FAISS index + associated metadata to self.index_path.
        """
        data = {
            "faiss_index": self.index,
            "vectors": self.vectors,
            "metadata": self.metadata
        }
        with open(self.index_path, "wb") as f:
            pickle.dump(data, f)
