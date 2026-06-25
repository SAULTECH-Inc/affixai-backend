# AI Document Signer - FastAPI AI Engine

## Overview

This is the **AI & Document Intelligence Engine** for the AI-powered document automation platform. Built with FastAPI (Python), it handles:

- 🔍 **OCR Processing** - Text extraction from documents
- 🧠 **Field Detection** - Intelligent field identification
- 📍 **Auto-Placement** - Smart data positioning
- 📊 **Document Classification** - Automatic document type detection
- ✨ **Confidence Scoring** - Quality metrics for extractions

## Architecture

```
┌─────────────────────────────────────────────────────┐
│           FastAPI AI Engine                         │
│  (OCR, NLP, Field Detection, Classification)        │
└─────────────────────────────────────────────────────┘
              │                    │
              ▼                    ▼
    ┌─────────────────┐   ┌──────────────────┐
    │  Redis Cache    │   │  MongoDB Store   │
    │  (OCR Results)  │   │  (Processing)    │
    └─────────────────┘   └──────────────────┘
```

## Features

### ✅ Implemented

1. **OCR Processing**
   - Multiple OCR engines (Tesseract, EasyOCR)
   - PDF and image support
   - Multi-page document handling
   - Image preprocessing (denoising, deskewing)
   - Text extraction with bounding boxes
   - Confidence scoring

2. **Field Detection**
   - Pattern-based field identification
   - NLP-powered field classification
   - Field type detection (date, email, phone, etc.)
   - Context-aware field naming

3. **Auto-Fill Analysis**
   - Field matching with user data
   - Fuzzy matching algorithm
   - Synonym detection
   - Position mapping
   - Confidence scoring

4. **Document Classification**
   - Keyword-based classification
   - Layout analysis
   - Support for multiple document types
   - Confidence scoring

5. **API Security**
   - API key authentication
   - Rate limiting
   - CORS configuration

## Getting Started

### Prerequisites

- Python 3.11+
- [Poetry](https://python-poetry.org/docs/#installation) 2.x (dependency management)
- Tesseract OCR (for OCR processing)
- Redis (for caching)
- MongoDB (optional, for storing results)
- Docker & Docker Compose (optional)

### Installation

```bash
# Install Poetry (if not already installed)
curl -sSL https://install.python-poetry.org | python3 -

# Install Tesseract OCR
# macOS:
brew install tesseract poppler

# Ubuntu/Debian:
sudo apt-get install tesseract-ocr tesseract-ocr-eng poppler-utils

# Windows:
# Download from: https://github.com/UB-Mannheim/tesseract/wiki

# Install Python dependencies (creates a managed virtualenv)
poetry install

# Copy environment variables
cp .env.example .env

# Edit .env with your configuration
nano .env
```

### Running the Application

```bash
# Development mode (inside Poetry's managed environment)
poetry run uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Or activate the shell first
poetry shell
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Or run the entry script
poetry run python main.py
```

The API will be available at:
- **API**: http://localhost:8000
- **Swagger Docs**: http://localhost:8000/docs
- **ReDoc**: http://localhost:8000/redoc

### Using Docker

```bash
# Start all services
docker-compose up -d

# View logs
docker-compose logs -f fastapi

# Stop services
docker-compose down
```

## Environment Variables

Key environment variables to configure:

```env
# API Security
INTERNAL_API_KEY=your-secret-key-for-nestjs

# AWS S3 (for document access)
AWS_ACCESS_KEY_ID=your-aws-key
AWS_SECRET_ACCESS_KEY=your-aws-secret
AWS_S3_BUCKET=your-bucket-name

# OCR Settings
OCR_ENGINE=tesseract  # or easyocr
OCR_LANGUAGE=eng
OCR_DPI=300

# Redis (for caching)
REDIS_HOST=localhost
REDIS_PORT=6379
```

## API Documentation

Once running, visit **http://localhost:8000/docs** for interactive Swagger documentation.

### Key Endpoints

#### OCR Processing
```bash
POST /api/ocr/process
```
Process a document with OCR and extract fields with positions.

**Request**:
```json
{
  "documentUrl": "https://example.com/document.pdf",
  "documentType": "passport",
  "userId": "user-id-here"
}
```

**Response**:
```json
{
  "extractedFields": [
    {
      "fieldName": "name",
      "value": "John Doe",
      "confidence": 0.95,
      "position": {
        "x": 0.1,
        "y": 0.2,
        "width": 0.3,
        "height": 0.05,
        "page": 0
      }
    }
  ],
  "documentMetadata": {
    "pageCount": 1,
    "dimensions": {"width": 1200, "height": 1600}
  },
  "overallConfidence": 0.92
}
```

#### Auto-Fill Analysis
```bash
POST /api/autofill/analyze
```
Analyze document and match with user data for auto-fill.

**Request**:
```json
{
  "documentUrl": "https://example.com/form.pdf",
  "documentType": "form",
  "userData": {
    "firstName": "John",
    "lastName": "Doe",
    "dateOfBirth": "1990-01-01",
    "email": "john@example.com"
  }
}
```

**Response**:
```json
{
  "fieldPlacements": [
    {
      "fieldName": "firstName",
      "value": "John",
      "x": 0.15,
      "y": 0.25,
      "width": 0.3,
      "height": 0.05,
      "page": 0,
      "confidence": 0.95
    }
  ],
  "matchedFields": 4,
  "unmatchedFields": []
}
```

#### Document Classification
```bash
POST /api/classify/document
```
Automatically detect document type.

**Request**:
```json
{
  "documentUrl": "https://example.com/document.pdf"
}
```

**Response**:
```json
{
  "documentType": "passport",
  "confidence": 0.89,
  "possibleTypes": [
    {"type": "passport", "confidence": 0.89},
    {"type": "id_card", "confidence": 0.45}
  ]
}
```

## Project Structure

```
app/
├── api/
│   └── routes/
│       ├── ocr.py              # OCR endpoints
│       ├── autofill.py         # Auto-fill endpoints
│       ├── classify.py         # Classification endpoints
│       └── health.py           # Health check
├── core/
│   ├── config.py               # Configuration
│   └── security.py             # API key auth
├── services/
│   ├── ocr_service.py          # OCR engines
│   ├── document_processor.py  # Document processing
│   ├── autofill_service.py    # Auto-fill logic
│   └── classification_service.py  # Classification
└── models/
    └── schemas.py              # Pydantic models
```

## OCR Engines

### Tesseract (Default)
- Fast and accurate
- Good for printed text
- Supports 100+ languages
- Free and open-source

### EasyOCR (Alternative)
- Deep learning-based
- Better for handwritten text
- Slower but more accurate
- Requires GPU for optimal performance

To switch engines, update `.env`:
```env
OCR_ENGINE=easyocr
```

## Performance Optimization

### Caching
OCR results are cached in Redis to avoid reprocessing:
```python
# Results cached for 1 hour by default
CACHE_TTL=3600
```

### Async Processing
All document processing is asynchronous:
```python
ASYNC_PROCESSING=True
```

### Image Preprocessing
Images are preprocessed for better OCR:
- Grayscale conversion
- Noise reduction
- Adaptive thresholding
- Deskewing

## Integration with NestJS

The FastAPI engine communicates with NestJS backend:

```python
# NestJS calls FastAPI
POST http://localhost:8000/api/ocr/process
Headers:
  X-API-Key: internal-secret-key
```

## Error Handling

All endpoints return consistent error responses:

```json
{
  "detail": "Error message here"
}
```

Common status codes:
- `200` - Success
- `400` - Bad request
- `401` - Invalid API key
- `500` - Internal server error

## Testing

```bash
# Run tests
pytest

# With coverage
pytest --cov=app tests/

# Specific test file
pytest tests/test_ocr.py
```

## Monitoring

Health check endpoints:
- `GET /health` - Full health check
- `GET /health/ready` - Readiness probe
- `GET /health/live` - Liveness probe

## Logging

Logs are written to:
- Console (stdout)
- File: `logs/fastapi.log`

Log levels: DEBUG, INFO, WARNING, ERROR, CRITICAL

## Production Deployment

### Using Docker

```bash
# Build image
docker build -t ai-doc-signer-fastapi .

# Run container
docker run -d \
  -p 8000:8000 \
  --env-file .env \
  ai-doc-signer-fastapi
```

### Using Kubernetes

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fastapi-ai-engine
spec:
  replicas: 3
  template:
    spec:
      containers:
      - name: fastapi
        image: ai-doc-signer-fastapi:latest
        ports:
        - containerPort: 8000
```

## Performance Benchmarks

Typical processing times (single page):
- OCR extraction: 2-5 seconds
- Auto-fill analysis: 3-7 seconds
- Document classification: 2-4 seconds

With caching:
- Cached OCR: < 100ms
- Cached classification: < 50ms

## Troubleshooting

### Tesseract not found
```bash
# macOS
brew install tesseract

# Linux
sudo apt-get install tesseract-ocr
```

### Redis connection error
```bash
# Start Redis
docker run -d -p 6379:6379 redis:7-alpine
```

### Low OCR accuracy
- Increase DPI: `OCR_DPI=600`
- Try different engine: `OCR_ENGINE=easyocr`
- Check image quality

## Next Steps

1. ✅ Implement advanced NLP field detection
2. ✅ Add layout analysis with LayoutParser
3. ✅ Implement ML-based field prediction
4. ✅ Add support for more document types
5. ✅ Optimize for GPU processing

## Contributing

This is part of a larger project. Please coordinate before making changes.

## License

Proprietary - All rights reserved
