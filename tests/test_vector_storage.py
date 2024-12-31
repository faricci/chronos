# tests/test_vector_storage.py

import unittest
import os
import shutil
import numpy as np
from modules.vector_storage import FaissVectorStore

class TestFaissVectorStore(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        """
        Create a temporary directory or file path for FAISS index.
        """
        cls.test_index_path = "test_faiss_index.index"
        # Ensure it's removed if leftover from previous test
        if os.path.exists(cls.test_index_path):
            os.remove(cls.test_index_path)

    @classmethod
    def tearDownClass(cls):
        """
        Clean up the test index file if it exists.
        """
        if os.path.exists(cls.test_index_path):
            os.remove(cls.test_index_path)

    def test_add_vectors(self):
        store = FaissVectorStore(index_path=self.test_index_path, embedding_dim=3)
        vectors = [
            np.array([1.0, 0.0, 0.0]),
            np.array([0.0, 1.0, 0.0]),
            np.array([0.0, 0.0, 1.0])
        ]
        meta = [{"id": "vec1"}, {"id": "vec2"}, {"id": "vec3"}]

        store.add_vectors(vectors, meta)
        self.assertEqual(len(store.vectors), 3, "Should have added 3 vectors.")
        self.assertEqual(len(store.metadata), 3, "Should have 3 metadata entries.")

    def test_similarity_search(self):
        store = FaissVectorStore(index_path=self.test_index_path, embedding_dim=3)
        
        # Add known vectors
        vectors = [
            np.array([1.0, 0.0, 0.0]),
            np.array([0.0, 1.0, 0.0]),
            np.array([0.0, 0.0, 1.0])
        ]
        meta = [{"id": "x"}, {"id": "y"}, {"id": "z"}]
        store.add_vectors(vectors, meta)

        # Query with [1.0, 0.0, 0.0] => expect top result to be meta "x"
        query = np.array([1.0, 0.0, 0.0])
        results = store.similarity_search(query, k=2)
        self.assertGreaterEqual(len(results), 1, "Expect at least 1 result.")
        # Check the top result
        dist, m = results[0]
        self.assertEqual(m["id"], "x", "Closest vector should match 'x' metadata.")
        self.assertAlmostEqual(dist, 0.0, places=6, 
                               msg="Distance to identical vector should be near 0.")

    def test_save_and_load_index(self):
        # Initialize store, add vectors
        store = FaissVectorStore(index_path=self.test_index_path, embedding_dim=2)
        vectors = [
            np.array([1.0, 2.0]),
            np.array([3.0, 4.0])
        ]
        meta = [{"info": "vec1"}, {"info": "vec2"}]
        store.add_vectors(vectors, meta)

        # Save
        store.save_index()
        self.assertTrue(os.path.exists(self.test_index_path))

        # Create new store => should load existing index
        store2 = FaissVectorStore(index_path=self.test_index_path, embedding_dim=2)
        
        # Confirm we can do a similarity search
        query = np.array([1.0, 2.0])
        results = store2.similarity_search(query, k=1)
        self.assertEqual(len(results), 1)
        dist, m = results[0]
        self.assertAlmostEqual(dist, 0.0, places=5)
        self.assertEqual(m["info"], "vec1")


# If you want to run ONLY this file’s tests directly:
# if __name__ == "__main__":
#     runner = unittest.TextTestRunner(verbosity=2)
#     runner.run(TestFaissVectorStore.suite())

