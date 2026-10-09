---
description: Install RAG dependencies (ChromaDB with ONNX embeddings) for vector search
allowed-tools: Bash
---

# Install RAG Dependencies

Install optional RAG dependency (ChromaDB with built-in ONNX embeddings) for enhanced vector search
in the Claudmaster AI DM module indexing system. No torch or sentence-transformers needed.

## Instructions

### 1. Check if RAG is already installed

Run this Python check (use `uv run` in developer mode to check inside the project venv):
```bash
uv run python3 -c "import chromadb; print('OK')" 2>/dev/null && echo "INSTALLED" || echo "NOT_INSTALLED"
```

If already installed, tell the user:
> RAG dependencies are already installed. ChromaDB with ONNX embeddings is available.
> The Claudmaster AI DM is using vector search for module indexing.

And stop here.

### 2. Detect installation mode

Check for `pyproject.toml` in the project root to determine if this is a developer or user installation:

```bash
if [ -f "pyproject.toml" ] && grep -q 'name = "dm20-protocol"' pyproject.toml 2>/dev/null; then
    echo "MODE=developer"
else
    echo "MODE=user"
fi
```

### 3. Confirm with user

Before installing, inform the user:
> This will install ChromaDB with ONNX-based embeddings (~200MB download).
> These enable semantic vector search for the Claudmaster AI DM module indexing.
> Without them, everything works — the system falls back to TF-IDF keyword search.
>
> Note: No torch or sentence-transformers required — embeddings use lightweight ONNX runtime.
> If your platform/Python combo causes issues, the installer will auto-retry with Python 3.12.
>
> Proceed?

### 4. Install based on mode (with automatic fallback)

The install uses a fallback chain: try current Python first, then retry with Python 3.12 if needed.

#### Developer mode
```bash
# Attempt 1: current Python
uv sync --extra rag 2>&1 && echo "RAG_OK" || {
    echo "RETRY_312"
    # Attempt 2: pin Python 3.12 (broader ML library compatibility)
    uv sync --python 3.12 --extra rag 2>&1 && echo "RAG_OK_312" || echo "RAG_FAILED"
}
```

#### User mode
```bash
# Attempt 1: current Python
uv tool install "dm20-protocol[rag] @ git+https://github.com/Polloinfilzato/dm20-protocol.git" --force 2>&1 && echo "RAG_OK" || {
    echo "RETRY_312"
    # Attempt 2: pin Python 3.12
    uv tool install --python 3.12 "dm20-protocol[rag] @ git+https://github.com/Polloinfilzato/dm20-protocol.git" --force 2>&1 && echo "RAG_OK_312" || echo "RAG_FAILED"
}
```

### 5. Verify installation

After install completes, verify:
```bash
uv run python3 -c "import chromadb; from chromadb.utils.embedding_functions import DefaultEmbeddingFunction; print('OK')" 2>/dev/null && echo "SUCCESS" || echo "FAILED"
```

### 6. Report result

**On success (including Python 3.12 fallback):**
> RAG dependencies installed successfully.
> The Claudmaster AI DM will now use ChromaDB vector search with ONNX embeddings for module indexing.
> No restart needed — changes take effect on the next `ask_books` or module search call.

If Python 3.12 fallback was used, add:
> Note: Python 3.12 was used for broader library compatibility. This is handled automatically.

**On failure:**
> Installation failed. The server continues to work normally using TF-IDF search.
> You can try again later with `/dm:install-rag`.
>
> Common cause: your platform (macOS Intel / very new Python) may not have compatible
> ML library wheels. The TF-IDF search works great for most use cases.
