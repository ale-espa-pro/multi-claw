import unittest
from types import SimpleNamespace

from tools.memoryTools.semantic_splitter import count_tokens, semantic_split


class _FakeEmbeddings:
    def __init__(self, max_tokens: int):
        self.max_tokens = max_tokens
        self.calls: list[list[str]] = []

    def create(self, input, model):
        texts = input if isinstance(input, list) else [input]
        self.calls.append(texts)
        for text in texts:
            tokens = count_tokens(text)
            if tokens > self.max_tokens:
                raise AssertionError(f"embedding input has {tokens} tokens")

        return SimpleNamespace(
            data=[
                SimpleNamespace(index=idx, embedding=[1.0, float(idx + 1), 0.5])
                for idx, _ in enumerate(texts)
            ]
        )


class _FakeClient:
    def __init__(self, max_tokens: int):
        self.embeddings = _FakeEmbeddings(max_tokens)


class SemanticSplitterTests(unittest.TestCase):
    def test_long_segment_is_split_before_embedding(self):
        max_tokens = 20
        client = _FakeClient(max_tokens)
        long_tool_output = " ".join(["tooloutput"] * 200)

        result = semantic_split(
            [long_tool_output],
            client,
            min_tokens=1,
            max_tokens=max_tokens,
            overlap_tokens=0,
        )[0]

        self.assertGreater(len(result.chunk_texts), 1)
        self.assertTrue(all(tokens <= max_tokens for tokens in result.chunk_tokens))
        self.assertGreaterEqual(len(client.embeddings.calls), 2)

    def test_two_piece_long_segment_does_not_reuse_original_text(self):
        max_tokens = 20
        client = _FakeClient(max_tokens)
        long_tool_output = " ".join(["tooloutput"] * 35)

        result = semantic_split(
            [long_tool_output],
            client,
            min_tokens=1,
            max_tokens=max_tokens,
            overlap_tokens=0,
        )[0]

        self.assertGreater(len(result.chunk_texts), 1)
        self.assertTrue(all(tokens <= max_tokens for tokens in result.chunk_tokens))


if __name__ == "__main__":
    unittest.main()
