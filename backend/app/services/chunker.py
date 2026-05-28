from __future__ import annotations

from dataclasses import dataclass

from llama_index.core import Document as LlamaDocument
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.utils import get_tokenizer

CHUNK_SIZE = 512
CHUNK_OVERLAP = 128


@dataclass
class ChunkPayload:
    index: int
    content: str
    token_count: int


_splitter = SentenceSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
_tokenizer = get_tokenizer()


def _count_tokens(text: str) -> int:
    return len(_tokenizer(text))


def chunk_text(text: str) -> list[ChunkPayload]:
    if not text.strip():
        return []
    nodes = _splitter.get_nodes_from_documents([LlamaDocument(text=text)])
    return [
        ChunkPayload(index=i, content=node.get_content(), token_count=_count_tokens(node.get_content()))
        for i, node in enumerate(nodes)
    ]
