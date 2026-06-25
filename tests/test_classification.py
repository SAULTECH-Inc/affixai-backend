import pytest
from app.services.classification_service import DocumentClassifier
from app.models.schemas import DocumentType

class MockField:
    def __init__(self, value, x=0, y=0):
        self.value = value
        self.position = type('obj', (object,), {'x': x, 'y': y})

class MockOCRResult:
    def __init__(self, text_list):
        self.extractedFields = [MockField(t) for t in text_list]
        self.documentMetadata = type('obj', (object,), {
            'dimensions': {'width': 1000, 'height': 800},
            'pageCount': 1
        })

@pytest.mark.asyncio
async def test_classification_logic():
    classifier = DocumentClassifier()
    
    # Test Passport Keywords
    text = "PASSPORT Republic of Utopia Passport No 12345"
    score = classifier._calculate_type_score(text.lower(), classifier.document_keywords[DocumentType.PASSPORT])
    assert score > 0
    
    # Test Layout Analysis (Passport MRZ)
    ocr_result = MockOCRResult(["P<UTO12345<<", "Passport"])
    # Mock positions for MRZ (bottom of page)
    ocr_result.extractedFields[0].position.y = 0.8
    
    boost = classifier._analyze_layout(ocr_result, DocumentType.PASSPORT)
    assert boost > 0
