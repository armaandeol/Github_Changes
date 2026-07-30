#!/usr/bin/env bash
set -e

echo "============================================="
echo "   GitHub PR Risk Detector & RAG Pipeline    "
echo "============================================="

# 1. Setup Python Virtual Environment
if [ ! -d ".venv" ]; then
    echo "[*] Creating Python virtual environment in .venv..."
    python3 -m venv .venv
else
    echo "[*] Virtual environment already exists."
fi

# Activate the virtual environment
source .venv/bin/activate

# Upgrade pip
echo "[*] Upgrading pip..."
pip install --upgrade pip > /dev/null

# 2. Install Dependencies
echo "[*] Installing project dependencies..."
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt > /dev/null
else
    echo "[!] requirements.txt not found!"
fi

echo "[*] Installing RAG-specific dependencies..."
if [ -f "RAG/requirements.txt" ]; then
    pip install -r RAG/requirements.txt > /dev/null
else
    echo "[!] RAG/requirements.txt not found!"
fi

# 3. Check for .env files
echo "[*] Verifying environment configurations..."
if [ ! -f ".env" ]; then
    echo "[!] Warning: Root .env file not found. You will need GITHUB_TOKEN and GEMINI_API_KEY."
fi

if [ ! -f "RAG/.env" ]; then
    echo "[!] Warning: RAG/.env file not found. You will need GROQ_API_KEY (or KIMI_API_KEY)."
fi

# 4. Initialize RAG Knowledge Base
if [ ! -d "RAG/chroma_kb" ]; then
    echo "[*] First time setup: Initializing and ingesting RAG knowledge base..."
    cd RAG
    python ingest.py
    cd ..
else
    echo "[*] RAG knowledge base already exists."
fi

# 5. Run the PR Detector Workflow
echo "============================================="
echo "[*] Running Pipeline..."
echo "============================================="

# If the user ran `./start.sh` without arguments, use the default repository:
if [ "$#" -eq 0 ]; then
    echo "[*] No arguments provided. Running default repository monitoring..."
    python pr_detector.py --owner armaandeol --repo Test_Repo --poll --interval 30 --comment --lines --features
else
    # Pass all bash script arguments directly to the python script
    python pr_detector.py "$@"
fi
