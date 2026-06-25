import pytesseract
import cv2
import numpy as np
from PIL import Image
from typing import List, Dict, Tuple, Optional
from loguru import logger
import io

from app.core.config import settings
from app.models.schemas import ExtractedField, FieldPosition


class OCREngine:
    """Base OCR Engine class"""
    
    def __init__(self):
        self.dpi = settings.OCR_DPI
        self.language = settings.OCR_LANGUAGE
    
    def preprocess_image(self, image: np.ndarray) -> np.ndarray:
        """Preprocess image for better OCR results"""
        # Convert to grayscale
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        
        # Apply denoising
        denoised = cv2.fastNlMeansDenoising(gray)
        
        # Apply adaptive threshold
        thresh = cv2.adaptiveThreshold(
            denoised, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            11, 2
        )
        
        # Deskew if needed
        thresh = self._deskew(thresh)
        
        return thresh
    
    def _deskew(self, image: np.ndarray) -> np.ndarray:
        """Deskew image"""
        coords = np.column_stack(np.where(image > 0))
        if len(coords) == 0:
            return image
        
        angle = cv2.minAreaRect(coords)[-1]
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle
        
        if abs(angle) < 0.5:  # Skip if angle is too small
            return image
        
        (h, w) = image.shape[:2]
        center = (w // 2, h // 2)
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(
            image, M, (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE
        )
        
        return rotated
    
    def extract_text_with_positions(
        self, image: np.ndarray, page_num: int = 0
    ) -> List[ExtractedField]:
        """Extract text with bounding box positions"""
        raise NotImplementedError


class TesseractEngine(OCREngine):
    """Tesseract OCR Engine"""
    
    def extract_text_with_positions(
        self, image: np.ndarray, page_num: int = 0
    ) -> List[ExtractedField]:
        """Extract text using Tesseract with position data"""
        
        # Preprocess image
        processed = self.preprocess_image(image)
        
        # Get detailed data from Tesseract
        data = pytesseract.image_to_data(
            processed,
            lang=self.language,
            output_type=pytesseract.Output.DICT,
            config='--psm 6'  # Assume uniform block of text
        )
        
        fields = []
        height, width = image.shape[:2]
        
        # Group text by lines
        current_line = []
        last_line_num = -1
        
        for i in range(len(data['text'])):
            if int(data['conf'][i]) < 0:  # Skip low confidence
                continue
            
            text = data['text'][i].strip()
            if not text:
                continue
            
            line_num = data['line_num'][i]
            
            # If new line, process previous line
            if line_num != last_line_num and current_line:
                fields.append(self._merge_line_to_field(
                    current_line, width, height, page_num
                ))
                current_line = []
            
            current_line.append({
                'text': text,
                'conf': data['conf'][i],
                'left': data['left'][i],
                'top': data['top'][i],
                'width': data['width'][i],
                'height': data['height'][i],
            })
            
            last_line_num = line_num
        
        # Process last line
        if current_line:
            fields.append(self._merge_line_to_field(
                current_line, width, height, page_num
            ))
        
        return fields
    
    def _merge_line_to_field(
        self,
        line_data: List[Dict],
        img_width: int,
        img_height: int,
        page_num: int
    ) -> ExtractedField:
        """Merge line data into a single field"""
        
        # Combine text
        text = ' '.join([item['text'] for item in line_data])
        
        # Calculate bounding box
        min_left = min([item['left'] for item in line_data])
        max_right = max([item['left'] + item['width'] for item in line_data])
        min_top = min([item['top'] for item in line_data])
        max_bottom = max([item['top'] + item['height'] for item in line_data])
        
        # Average confidence
        avg_conf = sum([item['conf'] for item in line_data]) / len(line_data) / 100
        
        # Normalize coordinates (0-1 range)
        position = FieldPosition(
            x=min_left / img_width,
            y=min_top / img_height,
            width=(max_right - min_left) / img_width,
            height=(max_bottom - min_top) / img_height,
            page=page_num
        )
        
        # Try to identify field name from text
        field_name = self._identify_field_name(text)
        
        return ExtractedField(
            fieldName=field_name,
            value=text,
            confidence=avg_conf,
            position=position,
            rawText=text
        )
    
    def _identify_field_name(self, text: str) -> str:
        """Identify field name from text content"""
        text_lower = text.lower()
        
        # Common field patterns
        if any(word in text_lower for word in ['name', 'nombre']):
            return 'name'
        elif any(word in text_lower for word in ['date of birth', 'dob', 'birth date']):
            return 'dateOfBirth'
        elif any(word in text_lower for word in ['address', 'dirección']):
            return 'address'
        elif any(word in text_lower for word in ['phone', 'telephone', 'tel']):
            return 'phone'
        elif any(word in text_lower for word in ['email', 'e-mail']):
            return 'email'
        elif any(word in text_lower for word in ['passport', 'passport number']):
            return 'passportNumber'
        elif any(word in text_lower for word in ['license', 'licence']):
            return 'licenseNumber'
        elif any(word in text_lower for word in ['ssn', 'social security']):
            return 'ssn'
        else:
            return 'unknown'


class EasyOCREngine(OCREngine):
    """EasyOCR Engine (alternative)"""
    
    def __init__(self):
        super().__init__()
        try:
            import easyocr
            self.reader = easyocr.Reader([self.language])
        except ImportError:
            logger.warning("EasyOCR not installed")
            self.reader = None
    
    def extract_text_with_positions(
        self, image: np.ndarray, page_num: int = 0
    ) -> List[ExtractedField]:
        """Extract text using EasyOCR"""
        if not self.reader:
            raise RuntimeError("EasyOCR not available")
        
        results = self.reader.readtext(image)
        fields = []
        height, width = image.shape[:2]
        
        for bbox, text, conf in results:
            # bbox is [[x1,y1], [x2,y2], [x3,y3], [x4,y4]]
            x_coords = [point[0] for point in bbox]
            y_coords = [point[1] for point in bbox]
            
            min_x = min(x_coords)
            max_x = max(x_coords)
            min_y = min(y_coords)
            max_y = max(y_coords)
            
            position = FieldPosition(
                x=min_x / width,
                y=min_y / height,
                width=(max_x - min_x) / width,
                height=(max_y - min_y) / height,
                page=page_num
            )
            
            field_name = self._identify_field_name(text)
            
            fields.append(ExtractedField(
                fieldName=field_name,
                value=text,
                confidence=conf,
                position=position,
                rawText=text
            ))
        
        return fields
    
    def _identify_field_name(self, text: str) -> str:
        """Same as Tesseract"""
        text_lower = text.lower()
        
        if any(word in text_lower for word in ['name', 'nombre']):
            return 'name'
        elif any(word in text_lower for word in ['date of birth', 'dob']):
            return 'dateOfBirth'
        elif any(word in text_lower for word in ['address']):
            return 'address'
        else:
            return 'unknown'


def get_ocr_engine() -> OCREngine:
    """Get OCR engine based on configuration"""
    engine_name = settings.OCR_ENGINE.lower()
    
    if engine_name == "tesseract":
        return TesseractEngine()
    elif engine_name == "easyocr":
        return EasyOCREngine()
    else:
        logger.warning(f"Unknown OCR engine: {engine_name}, using Tesseract")
        return TesseractEngine()
