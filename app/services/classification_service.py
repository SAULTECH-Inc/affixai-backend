from typing import List, Dict
from loguru import logger
import re

from app.models.schemas import DocumentType, ClassifyDocumentResponse
from app.services.document_processor import document_processor


class DocumentClassifier:
    """Service for classifying document types"""
    
    def __init__(self):
        # Keywords for each document type
        self.document_keywords = {
            DocumentType.PASSPORT: [
                'passport', 'passeport', 'pasaporte', 'nationality',
                'passport no', 'travel document', 'issuing authority',
                'p<', 'republic', 'date of birth', 'sex', 'signature'
            ],
            DocumentType.ID_CARD: [
                'identity card', 'id card', 'national id', 'citizen card',
                'identification', 'carte d\'identité', 'residence permit',
                'personal number', 'card no'
            ],
            DocumentType.DRIVERS_LICENSE: [
                'driver', 'license', 'licence', 'driving', 'motor vehicle',
                'class', 'restrictions', 'endorsements', 'dl no', 'issuing state',
                'operator', 'donor'
            ],
            DocumentType.FORM: [
                'form', 'application', 'please complete', 'section',
                'instructions', 'signature', 'date', 'checkbox', 'print name',
                'official use only', 'applicant'
            ],
            DocumentType.CONTRACT: [
                'contract', 'agreement', 'party', 'whereas', 'hereby',
                'terms and conditions', 'effective date', 'termination',
                'confidentiality', 'governing law', 'witness', 'parties'
            ],
            DocumentType.APPLICATION: [
                'application', 'apply', 'applicant', 'position',
                'employment', 'admission', 'enrollment', 'personal details',
                'education', 'reference'
            ],
            DocumentType.CERTIFICATE: [
                'certificate', 'certification', 'certify', 'awarded',
                'completion', 'achievement', 'diploma', 'completed',
                'degree', 'honor'
            ],
        }
    
    async def classify_document(
        self, document_url: str
    ) -> ClassifyDocumentResponse:
        """Classify document type"""
        
        logger.info(f"Classifying document: {document_url}")
        
        # Extract text from document
        ocr_result = await document_processor.process_document(
            document_url,
            'other'  # Initial type
        )
        
        # Combine all extracted text
        all_text = ' '.join([
            field.value.lower()
            for field in ocr_result.extractedFields
        ])
        
        # Score each document type
        scores = {}
        for doc_type, keywords in self.document_keywords.items():
            score = self._calculate_type_score(all_text, keywords)
            scores[doc_type] = score
        
        # Get best match
        best_type = max(scores, key=scores.get)
        best_score = scores[best_type]
        
        # Get top 3 possible types
        sorted_scores = sorted(
            scores.items(),
            key=lambda x: x[1],
            reverse=True
        )[:3]
        
        possible_types = [
            {'type': doc_type.value, 'confidence': score}
            for doc_type, score in sorted_scores
        ]
        
        # Apply layout analysis for additional confidence
        # layout_boost = self._analyze_layout(ocr_result, best_type)
        # final_confidence = min(best_score + layout_boost, 1.0)
        
        # We need to make sure _analyze_layout is called
        layout_boost = self._analyze_layout(ocr_result, best_type)
        final_confidence = min(best_score + layout_boost, 1.0)
        
        logger.info(
            f"Classified as {best_type.value} "
            f"with {final_confidence:.2%} confidence"
        )
        
        return ClassifyDocumentResponse(
            documentType=best_type,
            confidence=final_confidence,
            possibleTypes=possible_types
        )
    
    def _calculate_type_score(
        self, text: str, keywords: List[str]
    ) -> float:
        """Calculate score for a document type based on keywords"""
        
        matches = 0
        for keyword in keywords:
            if keyword.lower() in text:
                matches += 1
        
        # Normalize score
        score = matches / len(keywords) if keywords else 0
        
        return score

    def _analyze_layout(self, ocr_result, suggested_type: DocumentType) -> float:
        """
        Analyze document layout for additional classification confidence.
        Uses heuristics like aspect ratio, text density, and specific field positions.
        """
        
        fields = ocr_result.extractedFields
        metadata = ocr_result.documentMetadata
        
        # Dimensions check (Passports/IDs are usually landscape-ish or specific ratios)
        width = metadata.dimensions.get('width', 0)
        height = metadata.dimensions.get('height', 0)
        aspect_ratio = width / height if height > 0 else 0
        
        # Passport: Usually ID-3 format (125 × 88 mm) ~ 1.42 aspect ratio
        if suggested_type == DocumentType.PASSPORT:
            # Check for MRZ (Machine Readable Zone)
            # MRZ is usually at the bottom, monospaced, lots of <<<<
            has_mrz = any(
                '<<' in f.value and f.position.y > 0.6
                for f in fields
            )
            if has_mrz:
                return 0.3
            
            # Check for photo-like blank area in top section
            has_top_blank = any(
                f.position.y < 0.3 and f.position.x < 0.3
                for f in fields
            )
            return 0.1 if has_top_blank else 0.0
        
        # ID Card: Also small, often landscape
        if suggested_type == DocumentType.ID_CARD:
            if 1.3 < aspect_ratio < 1.7:
                 return 0.1
        
        # Form: Usually has many fields and labels
        if suggested_type == DocumentType.FORM:
            # Check for structured layout with many fields
            if len(fields) > 20:
                return 0.2
            if any('form' in f.value.lower() for f in fields[:5]): # Header check
                return 0.15
        
        # Contract: Usually long text, multiple pages, dense text
        if suggested_type == DocumentType.CONTRACT:
            if metadata.pageCount > 2:
                return 0.15
            
            # High text density
            total_text_len = sum(len(f.value) for f in fields)
            if total_text_len > 2000:
                return 0.1
        
        return 0.0


# Singleton instance
document_classifier = DocumentClassifier()
