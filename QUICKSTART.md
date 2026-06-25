# Quick Start Guide - FastAPI AI Engine

## 🚀 Getting Started in 5 Minutes

### Option 1: Docker (Easiest)

```bash
# 1. Clone and navigate
cd ai-document-signer-fastapi

# 2. Create .env file
cp .env.example .env

# 3. Update INTERNAL_API_KEY in .env
# This should match the FASTAPI_API_KEY in NestJS .env

# 4. Start everything
docker-compose up -d

# That's it! Service running on http://localhost:8000
```

### Option 2: Local Development

```bash
# 1. Install Tesseract OCR
# macOS:
brew install tesseract poppler

# Ubuntu/Debian:
sudo apt-get install tesseract-ocr tesseract-ocr-eng poppler-utils

# 2. Install Poetry (if not already installed)
curl -sSL https://install.python-poetry.org | python3 -

# 3. Install dependencies (Poetry creates a managed virtualenv)
poetry install

# 4. Create .env
cp .env.example .env
# Edit .env with your settings

# 5. Run the application
poetry run uvicorn main:app --reload
```

## ✅ Verify Installation

Visit these URLs:

- **API Root**: http://localhost:8000
- **Swagger Docs**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health

## 🧪 Test the API

### 1. Health Check

```bash
curl http://localhost:8000/health
```

Expected response:
```json
{
  "status": "healthy",
  "version": "1.0.0",
  "ocrEngine": "tesseract",
  "services": {
    "ocr": "healthy",
    "redis": "healthy",
    "s3": "healthy"
  }
}
```

### 2. Test OCR Processing

```bash
curl -X POST http://localhost:8000/api/ocr/process \
  -H "Content-Type: application/json" \
  -H "X-API-Key: internal-secret-key-for-nestjs-communication" \
  -d '{
    "documentUrl": "https://example.com/sample.pdf",
    "documentType": "form",
    "userId": "test-user"
  }'
```

### 3. Test Document Classification

```bash
curl -X POST http://localhost:8000/api/classify/document \
  -H "Content-Type: application/json" \
  -H "X-API-Key: internal-secret-key-for-nestjs-communication" \
  -d '{
    "documentUrl": "https://example.com/sample.pdf"
  }'
```

### 4. Test Auto-Fill

```bash
curl -X POST http://localhost:8000/api/autofill/analyze \
  -H "Content-Type: application/json" \
  -H "X-API-Key: internal-secret-key-for-nestjs-communication" \
  -d '{
    "documentUrl": "https://example.com/form.pdf",
    "documentType": "form",
    "userData": {
      "firstName": "John",
      "lastName": "Doe",
      "email": "john@example.com"
    }
  }'
```

## 🔧 Configuration

### Required Environment Variables

```env
# API Security (MUST MATCH NestJS)
INTERNAL_API_KEY=internal-secret-key-for-nestjs-communication

# AWS S3 (for document access)
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_S3_BUCKET=your-bucket-name
```

### Optional Settings

```env
# OCR Engine (tesseract or easyocr)
OCR_ENGINE=tesseract

# OCR Quality
OCR_DPI=300
OCR_LANGUAGE=eng

# Redis (if running separately)
REDIS_HOST=localhost
REDIS_PORT=6379
```

## 🔗 Integration with NestJS

### 1. Start FastAPI (port 8000)
```bash
docker-compose up -d
# or
uvicorn main:app --reload
```

### 2. Configure NestJS
In NestJS `.env`:
```env
FASTAPI_URL=http://localhost:8000
FASTAPI_API_KEY=internal-secret-key-for-nestjs-communication
```

### 3. Test Integration
Upload a document in NestJS - it will automatically call FastAPI for processing!

## 📊 Using Swagger UI

1. Open http://localhost:8000/docs
2. Click "Authorize" button
3. Enter API key: `internal-secret-key-for-nestjs-communication`
4. Try the endpoints interactively!

## 🐛 Common Issues

### Tesseract not found
```
Error: tesseract is not installed
```
**Solution**:
```bash
# macOS
brew install tesseract

# Ubuntu/Debian
sudo apt-get install tesseract-ocr tesseract-ocr-eng

# Windows
# Download from: https://github.com/UB-Mannheim/tesseract/wiki
```

### Poppler not found (PDF processing)
```
Error: Unable to get page count. Is poppler installed?
```
**Solution**:
```bash
# macOS
brew install poppler

# Ubuntu/Debian
sudo apt-get install poppler-utils

# Windows
# Download from: https://github.com/oschwartz10612/poppler-windows
```

### Redis connection error
```
Error: Could not connect to Redis
```
**Solution**:
```bash
# Using Docker
docker-compose up -d redis

# Or install locally
# macOS: brew install redis
# Ubuntu: sudo apt-get install redis-server
```

### Low OCR accuracy
**Solutions**:
1. Increase DPI: `OCR_DPI=600` in .env
2. Try EasyOCR: `OCR_ENGINE=easyocr`
3. Ensure good image quality
4. Check document preprocessing

### Import errors
```
ModuleNotFoundError: No module named 'XXX'
```
**Solution**:
```bash
poetry install
```

## 📁 File Structure

```
ai-document-signer-fastapi/
├── main.py                 # Application entry point
├── pyproject.toml          # Poetry project + dependencies
├── poetry.lock             # Pinned dependency lockfile
├── .env.example            # Environment template
├── Dockerfile              # Docker configuration
├── docker-compose.yml      # Multi-service setup
└── app/
    ├── api/
    │   └── routes/        # API endpoints
    ├── services/          # Business logic
    ├── models/            # Data models
    └── core/              # Configuration
```

## 🎯 Next Steps

1. **Explore Swagger UI**: http://localhost:8000/docs
2. **Test with real documents**: Upload PDFs and images
3. **Check logs**: `docker-compose logs -f fastapi`
4. **Integrate with NestJS**: Connect the backend
5. **Monitor performance**: Check `/health` endpoint

## 📖 Full Documentation

See [README.md](./README.md) for comprehensive documentation.

## 🆘 Need Help?

1. Check the logs: `docker-compose logs fastapi`
2. Verify environment variables: `cat .env`
3. Test health endpoint: `curl http://localhost:8000/health`
4. Check Swagger docs: http://localhost:8000/docs

---

**Ready to process some documents!** 🚀
