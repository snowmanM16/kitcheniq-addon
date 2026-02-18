#!/bin/bash
# KitchenIQ - Setup & Run Script

echo "🍳 KitchenIQ Setup"
echo "=================="

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 not found. Please install Python 3.8+"
    exit 1
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
fi

# Activate venv
source venv/bin/activate

# Install requirements
echo "📥 Installing dependencies..."
pip install -r requirements.txt -q

# Check for API key
if [ -z "$OPENAI_API_KEY" ]; then
    echo ""
    echo "⚠️  OPENAI_API_KEY not set!"
    echo "   Set it with: export OPENAI_API_KEY=your_key_here"
    echo "   Or add it to a .env file"
    echo ""
fi

# Create .env if it doesn't exist
if [ ! -f ".env" ]; then
    echo "# Add your OpenAI API key here" > .env
    echo "OPENAI_API_KEY=your_key_here" >> .env
    echo "📝 Created .env file - add your API key"
fi

# Load .env
export $(grep -v '^#' .env | xargs) 2>/dev/null

echo ""
echo "✅ Starting KitchenIQ on http://localhost:5000"
echo "   Press Ctrl+C to stop"
echo ""

python3 app.py
