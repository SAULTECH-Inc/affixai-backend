import cv2
import numpy as np
from PIL import Image
from pdf2image import convert_from_bytes, convert_from_path
from typing import List, Dict, Optional
from loguru import logger
import httpx
import io
import boto3
from urllib.parse import urlparse

from app.core.config import settings
from app.models.schemas import (
    ExtractedField,
    DocumentMetadata,
    OCRProcessResponse
)
from app.services.ocr_service import get_ocr_engine
from app.services.layout_analyzer import layout_analyzer


class DocumentProcessor:
    """Document processing service"""
    
    def __init__(self):
        self.ocr_engine = get_ocr_engine()
        self.s3_client = boto3.client(
            's3',
            region_name=settings.AWS_REGION,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        )
    
    async def process_document(
        self,
        document_url: str,
        document_type: str
    ) -> OCRProcessResponse:
        """Main document processing pipeline"""
        
        logger.info(f"Processing document: {document_url}")
        
        # Step 1: Download document
        document_bytes = await self._download_document(document_url)
        
        # Step 2: Convert to images
        images = self._convert_to_images(document_bytes, document_url)
        
        # Step 3: Extract text from all pages
        all_fields = []
        for page_num, image in enumerate(images):
            fields = self.ocr_engine.extract_text_with_positions(
                image, page_num
            )
            all_fields.extend(fields)
        
        # Step 4: Post-process and enhance fields
        enhanced_fields = self._enhance_fields(all_fields, document_type)
        
        # Step 5: Calculate metadata
        metadata = self._calculate_metadata(images, document_bytes)
        
        # Step 6: Calculate overall confidence
        overall_confidence = self._calculate_overall_confidence(enhanced_fields)
        
        logger.info(
            f"Extracted {len(enhanced_fields)} fields with "
            f"{overall_confidence:.2%} confidence"
        )
        
        return OCRProcessResponse(
            extractedFields=enhanced_fields,
            documentMetadata=metadata,
            overallConfidence=overall_confidence
        )
    
    async def _download_document(self, url: str) -> bytes:
        """Download document from URL or S3"""
        
        parsed = urlparse(url)
        
        # Check if it's an S3 URL
        if 's3.amazonaws.com' in parsed.netloc or parsed.scheme == 's3':
            return self._download_from_s3(url)
        else:
            return await self._download_from_http(url)
    
    def _download_from_s3(self, url: str) -> bytes:
        """Download from S3"""
        # Extract bucket and key from URL
        parsed = urlparse(url)
        
        if 's3.amazonaws.com' in parsed.netloc:
            # Format: https://bucket.s3.region.amazonaws.com/key
            bucket = parsed.netloc.split('.')[0]
            key = parsed.path.lstrip('/')
        else:
            # Presigned URL - extract from query params
            bucket = settings.AWS_S3_BUCKET
            key = parsed.path.lstrip('/')
        
        logger.info(f"Downloading from S3: {bucket}/{key}")
        
        response = self.s3_client.get_object(Bucket=bucket, Key=key)
        return response['Body'].read()
    
    async def _download_from_http(self, url: str) -> bytes:
        """Download from HTTP/HTTPS"""
        async with httpx.AsyncClient() as client:
            response = await client.get(url, timeout=60.0)
            response.raise_for_status()
            return response.content
    
    def _convert_to_images(
        self, document_bytes: bytes, url: str
    ) -> List[np.ndarray]:
        """Convert document to images"""
        
        # Determine file type
        if url.lower().endswith('.pdf'):
            return self._pdf_to_images(document_bytes)
        else:
            return self._image_file_to_array(document_bytes)
    
    def _pdf_to_images(self, pdf_bytes: bytes) -> List[np.ndarray]:
        """Convert PDF to images"""
        images = convert_from_bytes(
            pdf_bytes,
            dpi=settings.OCR_DPI,
            fmt='jpeg'
        )
        
        # Convert PIL Images to numpy arrays
        np_images = []
        for img in images:
            np_img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
            np_images.append(np_img)
        
        return np_images
    
    def _image_file_to_array(self, image_bytes: bytes) -> List[np.ndarray]:
        """Convert image file to numpy array"""
        img = Image.open(io.BytesIO(image_bytes))
        np_img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        return [np_img]
    
    def _enhance_fields(
        self,
        fields: List[ExtractedField],
        document_type: str
    ) -> List[ExtractedField]:
        """Enhance extracted fields with NLP and pattern matching"""
        
        enhanced = []
        
        
        # First pass: clean and basic type detection
        for field in fields:
            # Apply field type detection
            field.fieldType = self._detect_field_type(field.value)
            
            # Clean and normalize value
            field.value = self._clean_field_value(field.value)
            
        # Second pass: Spatial Analysis for Field Naming
        # We try to find specific labels and associate them with values
        self._apply_spatial_labeling(fields, document_type)
            
        return fields

    def _apply_spatial_labeling(self, fields: List[ExtractedField], document_type: str):
        """Use spatial layout to identify fields based on labels"""
        
        # Define expected labels -> field names mapping
        # In a real app, this would be more extensive or config-driven
        label_map = {
            'passport no': 'passportNumber',
            'passport number': 'passportNumber',
            'surname': 'lastName',
            'given names': 'firstName',
            'date of birth': 'dateOfBirth',
            'nationality': 'nationality',
            'sex': 'sex',
            'place of birth': 'placeOfBirth',
            'date of issue': 'issueDate',
            'date of expiry': 'expiryDate',
            'authority': 'authority'
        }
        
        for label_text, target_field_name in label_map.items():
            # Find the value associated with this label
            value_field = layout_analyzer.find_value_for_label(label_text, fields)
            
            if value_field:
                # If we found a value near this label, update its name
                # Only update if it's currently unknown or less specific
                if value_field.fieldName == 'unknown' or value_field.fieldName == 'text':
                     value_field.fieldName = target_field_name
                     logger.info(f"Spatially identified {target_field_name}: {value_field.value}")
    
    def _detect_field_type(self, value: str) -> str:
        """Detect field type from value"""
        import re
        
        # Date patterns
        if re.match(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', value):
            return 'date'
        
        # Email pattern
        if re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', value):
            return 'email'
        
        # Phone pattern
        if re.match(r'[\d\s\-\(\)]{10,}', value):
            return 'phone'
        
        # Number
        if re.match(r'^\d+$', value):
            return 'number'
        
        return 'text'
    
    def _clean_field_value(self, value: str) -> str:
        """Clean and normalize field value"""
        # Remove extra whitespace
        cleaned = ' '.join(value.split())
        
        # Remove common OCR artifacts
        cleaned = cleaned.replace('|', 'I')
        cleaned = cleaned.replace('0', 'O') if not cleaned.isdigit() else cleaned
        
        return cleaned.strip()
    
    def _infer_field_name(self, value: str, document_type: str) -> str:
        """Infer field name from value and document type"""
        
        # Document type specific patterns
        if document_type == 'passport':
            return self._infer_passport_field(value)
        elif document_type == 'id_card':
            return self._infer_id_card_field(value)
        else:
            return 'unknown'
    
    def _infer_passport_field(self, value: str) -> str:
        """Infer field name for passport"""
        import re
        
        # Passport number pattern
        if re.match(r'^[A-Z]{1,2}\d{6,9}$', value):
            return 'passportNumber'
        
        # Date pattern
        if re.match(r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}', value):
            return 'dateOfBirth'
        
        # Country codes
        if len(value) == 3 and value.isupper():
            return 'nationality'
        
        return 'unknown'
    
    def _infer_id_card_field(self, value: str) -> str:
        """Infer field name for ID card"""
        # Similar logic for ID cards
        return 'unknown'
    
    def _calculate_metadata(
        self,
        images: List[np.ndarray],
        document_bytes: bytes
    ) -> DocumentMetadata:
        """Calculate document metadata"""
        
        first_image = images[0]
        height, width = first_image.shape[:2]
        
        return DocumentMetadata(
            pageCount=len(images),
            dimensions={'width': width, 'height': height},
            fileSize=len(document_bytes),
            language=settings.OCR_LANGUAGE
        )
    
    def _calculate_overall_confidence(
        self, fields: List[ExtractedField]
    ) -> float:
        """Calculate overall confidence score"""
        if not fields:
            return 0.0
        
        return sum(f.confidence for f in fields) / len(fields)


# Singleton instance
document_processor = DocumentProcessor()
