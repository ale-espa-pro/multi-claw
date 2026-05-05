import unittest

from tools.memoryTools.RAG_memory import MemoryRag


class MemoryRetrievalTests(unittest.TestCase):
    def test_hybrid_merge_deduplicates_and_tracks_methods(self):
        vector_results = [
            {
                "session_id": "s1",
                "message_order": 1,
                "chunck": "vector and keyword",
                "score": 0.8,
                "distance": 0.2,
            },
            {
                "session_id": "s2",
                "message_order": 1,
                "chunck": "vector only",
                "score": 0.7,
                "distance": 0.3,
            },
        ]
        keyword_results = [
            {
                "session_id": "s1",
                "message_order": 1,
                "chunck": "vector and keyword",
                "keyword_score": 1.4,
                "score": 1.4,
            },
            {
                "session_id": "s3",
                "message_order": 1,
                "chunck": "keyword only",
                "keyword_score": 1.0,
                "score": 1.0,
            },
        ]

        merged = MemoryRag._merge_ranked_chunks(
            vector_results=vector_results,
            keyword_results=keyword_results,
            limit=3,
        )

        self.assertEqual(merged[0]["session_id"], "s1")
        self.assertEqual(merged[0]["retrieval_method"], "keyword+vector")
        self.assertEqual(len(merged), 3)


if __name__ == "__main__":
    unittest.main()
