from typing import List, Dict, Any, Tuple, Optional
from fuzzywuzzy import fuzz
from loguru import logger
import re

from app.models.schemas import FieldPlacement, AutoFillResponse
from app.services.document_processor import document_processor
from app.services.layout_analyzer import layout_analyzer


class AutoFillService:
    """Service for analyzing documents and matching with user data"""
    
    def __init__(self):
        self.field_matching_threshold = 0.8
        
        # Field name mappings
        self.field_synonyms = {
            'firstName': ['first name', 'given name', 'name', 'forename'],
            'lastName': ['last name', 'surname', 'family name'],
            'fullName': ['full name', 'complete name', 'name'],
            'dateOfBirth': ['dob', 'birth date', 'date of birth', 'birthday'],
            'email': ['email address', 'e-mail', 'electronic mail'],
            'phone': ['phone number', 'telephone', 'mobile', 'cell'],
            'address': ['street address', 'home address', 'residence'],
            'city': ['city', 'town', 'municipality'],
            'state': ['state', 'province', 'region'],
            'zipCode': ['zip', 'postal code', 'postcode', 'zip code'],
            'country': ['country', 'nation', 'nationality'],
            'passportNumber': ['passport no', 'passport number', 'passport #'],
            'ssn': ['ssn', 'social security', 'social security number'],
        }
    
    async def analyze_for_autofill(
        self,
        document_url: str,
        document_type: str,
        user_data: Dict[str, Any]
    ) -> AutoFillResponse:
        """Analyze document and match with user data"""
        
        logger.info(f"Analyzing document for auto-fill: {document_url}")
        
        # Step 1: Process document to extract structure
        ocr_result = await document_processor.process_document(
            document_url,
            document_type
        )
        
        # Step 2: Match extracted fields with user data
        field_placements = []
        matched_fields = set()
        
        for extracted_field in ocr_result.extractedFields:
            # Try to match with user data
            match = self._find_matching_user_data(
                extracted_field.fieldName,
                extracted_field.value,
                user_data
            )
            
            if match:
                field_name, field_value, confidence = match
                
                # Create field placement
                placement = FieldPlacement(
                    fieldName=field_name,
                    value=str(field_value),
                    x=extracted_field.position.x,
                    y=extracted_field.position.y,
                    width=extracted_field.position.width,
                    height=extracted_field.position.height,
                    page=extracted_field.position.page,
                    confidence=confidence
                )
                
                field_placements.append(placement)
                matched_fields.add(field_name)
        
        # Find unmatched user data fields
        unmatched_fields = [
            key for key in user_data.keys()
            if key not in matched_fields
        ]
        
        # For unmatched fields, try to estimate position based on labels
        for unmatched_key in unmatched_fields:
             position = self._estimate_field_position(
                 unmatched_key,
                 ocr_result.extractedFields,
                 ocr_result.documentMetadata.dimensions
             )
             
             if position:
                 # Add suggested placement for unmatched field
                 # We mark confidence lower since it's an estimation
                 placement = FieldPlacement(
                    fieldName=unmatched_key,
                    value=str(user_data[unmatched_key]),
                    x=position['x'],
                    y=position['y'],
                    width=position['width'],
                    height=position['height'],
                    page=position['page'],
                    confidence=0.6  
                 )
                 field_placements.append(placement)
        
        logger.info(
            f"Matched {len(matched_fields)} fields, "
            f"Suggested {len(field_placements) - len(matched_fields)} placements for unmatched"
        )
        
        return AutoFillResponse(
            fieldPlacements=field_placements,
            matchedFields=len(matched_fields),
            unmatchedFields=[k for k in unmatched_fields if k not in [f.fieldName for f in field_placements]]
        )

    def _find_matching_user_data(
        self,
        field_name: str,
        field_value: str,
        user_data: Dict[str, Any]
    ) -> Tuple[str, str, float]:
        """Find matching user data for a field"""
        
        best_match = None
        best_score = 0.0
        
        for user_field_name, user_field_value in user_data.items():
            # Calculate similarity score
            score = self._calculate_field_similarity(
                field_name,
                field_value,
                user_field_name,
                str(user_field_value)
            )
            
            if score > best_score and score >= self.field_matching_threshold:
                best_score = score
                best_match = (user_field_name, str(user_field_value), score)
        
        return best_match
    
    def _calculate_field_similarity(
        self,
        extracted_field_name: str,
        extracted_field_value: str,
        user_field_name: str,
        user_field_value: str
    ) -> float:
        """Calculate similarity between extracted field and user data"""
        
        # Normalize names
        extracted_name_norm = extracted_field_name.lower().replace('_', ' ')
        user_name_norm = user_field_name.lower().replace('_', ' ')
        
        # Check if field names match directly
        if extracted_name_norm == user_name_norm:
            return 1.0
        
        # Check synonyms
        if self._are_synonyms(user_name_norm, extracted_name_norm):
            return 0.95
        
        # Use fuzzy matching on field names
        name_similarity = fuzz.ratio(extracted_name_norm, user_name_norm) / 100
        
        # If names are very similar, high confidence
        if name_similarity > 0.85:
            return name_similarity
        
        # Check value similarity (for validation)
        value_similarity = self._calculate_value_similarity(
            extracted_field_value,
            user_field_value
        )
        
        # Combine scores (weighted)
        combined_score = (name_similarity * 0.7) + (value_similarity * 0.3)
        
        return combined_score
    
    def _are_synonyms(self, field_name1: str, field_name2: str) -> bool:
        """Check if two field names are synonyms"""
        
        f1 = field_name1.lower()
        f2 = field_name2.lower()
        
        for canonical_name, synonyms in self.field_synonyms.items():
            c_name = canonical_name.lower()
            syns = [s.lower() for s in synonyms]
            
            # Check if both are in synonyms list
            if f1 in syns and f2 in syns:
                return True
            
            # Check if one is canonical and other is synonym
            if f1 == c_name and f2 in syns:
                return True
            if f2 == c_name and f1 in syns:
                return True
        
        return False
    
    def _calculate_value_similarity(
        self,
        value1: str,
        value2: str
    ) -> float:
        """Calculate similarity between two values"""
        
        # Normalize
        v1 = value1.lower().strip()
        v2 = value2.lower().strip()
        
        # Exact match
        if v1 == v2:
            return 1.0
        
        # Check if one contains the other
        if v1 in v2 or v2 in v1:
            return 0.9
        
        # Fuzzy matching
        return fuzz.ratio(v1, v2) / 100

    def _estimate_field_position(
        self,
        field_name: str,
        fields: List[Any],
        page_dimensions: Dict[str, float]
    ) -> Optional[Dict[str, float]]:
        """Estimate where a field should be placed (for unmatched fields)"""
        
        # 1. Try to find a label that matches the field name
        # We look for text that *looks like* the key (e.g. "Signature" for "signature")
        
        # Map user keys to probable document labels
        label_synonyms = self.field_synonyms.get(field_name, []) + [field_name]
        
        label_field = None
        for syn in label_synonyms:
            # We use a custom search that looks for the LABEL text
            # layout_analyzer._find_field_by_text is private but we can iterate
            for f in fields:
                if syn.lower() in f.value.lower():
                    label_field = f
                    break
            if label_field:
                break
        
        if label_field:
            # Found a label! Suggest a position to the RIGHT or BELOW
            # Default to right for most fields, bottom for signature/address usually
            
            is_large_block = field_name in ['address', 'signature']
            
            if is_large_block:
                # Place below
                return {
                    'x': label_field.position.x,
                    'y': label_field.position.y + label_field.position.height + 0.01,
                    'width': 0.3, # Standard width
                    'height': 0.05,
                    'page': label_field.position.page
                }
            else:
                # Place right
                return {
                    'x': label_field.position.x + label_field.position.width + 0.01,
                    'y': label_field.position.y,
                    'width': 0.2,
                    'height': label_field.position.height,
                     'page': label_field.position.page
                }
        
        return None


# Singleton instance
autofill_service = AutoFillService()
