from typing import List, Dict, Optional, Tuple
from app.models.schemas import ExtractedField, FieldPosition
from loguru import logger
import math

class SpatialLayoutAnalyzer:
    """
    Analyzes the spatial layout of extracted text to identify
    key-value pairs based on proximity and alignment.
    """

    def __init__(self):
        # Configuration for "nearby" detection
        self.horizontal_tolerance = 0.05  # 5% of page width
        self.vertical_tolerance = 0.02    # 2% of page height
        self.max_distance = 0.3           # Max distance to consider as a pair

    def find_value_for_label(
        self, 
        label_text: str, 
        all_fields: List[ExtractedField], 
        page_num: int = 0
    ) -> Optional[ExtractedField]:
        """
        Finds the value associated with a specific label text.
        (e.g., find the value next to "Date of Birth")
        """
        # 1. Find the label field
        label_field = self._find_field_by_text(label_text, all_fields, page_num)
        if not label_field:
            return None

        # 2. Find the best candidate value
        return self.find_value_near_label(label_field, all_fields)

    def find_value_near_label(
        self, 
        label_field: ExtractedField, 
        all_fields: List[ExtractedField]
    ) -> Optional[ExtractedField]:
        """
        Finds the field most likely to be the value for a given label field.
        Prioritizes:
        1. Right (same line)
        2. Below (immediately under)
        """
        candidates = []
        
        l_pos = label_field.position
        
        for field in all_fields:
            if field == label_field:
                continue
            
            if field.position.page != l_pos.page:
                continue

            f_pos = field.position
            
            # Check relation
            is_right = self._is_right_of(l_pos, f_pos)
            is_below = self._is_below(l_pos, f_pos)
            
            if is_right:
                dist = f_pos.x - (l_pos.x + l_pos.width)
                if dist < self.max_distance:
                    candidates.append(('right', dist, field))
            
            elif is_below:
                dist = f_pos.y - (l_pos.y + l_pos.height)
                # Stricter overlapping X constraint for "below"
                if self._is_aligned_vertically(l_pos, f_pos) and dist < self.max_distance:
                    candidates.append(('below', dist, field))

        if not candidates:
            return None

        # Sort candidates: prefer 'right' over 'below', then by distance
        # We give a 'penalty' to 'below' relationships to prefer horizontal pairs
        candidates.sort(key=lambda x: (0 if x[0] == 'right' else 1, x[1]))
        
        return candidates[0][2]

    def _find_field_by_text(
        self, 
        text: str, 
        fields: List[ExtractedField], 
        page_num: int
    ) -> Optional[ExtractedField]:
        """Find a field containing specific text (case-insensitive substring)"""
        text = text.lower()
        for field in fields:
            if field.position.page != page_num:
                continue
            if text in field.value.lower():
                return field
        return None

    def _is_right_of(self, origin: FieldPosition, target: FieldPosition) -> bool:
        """Check if target is to the right of origin (roughly same line)"""
        # Check vertical overlap (y-alignment)
        y_overlap = min(origin.y + origin.height, target.y + target.height) - max(origin.y, target.y)
        if y_overlap <= 0:
            return False
            
        # Check strictly right
        return target.x >= (origin.x + origin.width - self.horizontal_tolerance)

    def _is_below(self, origin: FieldPosition, target: FieldPosition) -> bool:
        """Check if target is below origin"""
        return target.y >= (origin.y + origin.height - self.vertical_tolerance)

    def _is_aligned_vertically(self, origin: FieldPosition, target: FieldPosition) -> bool:
        """Check if fields share horizontal space (x-alignment)"""
        x_overlap = min(origin.x + origin.width, target.x + target.width) - max(origin.x, target.x)
        return x_overlap > 0


layout_analyzer = SpatialLayoutAnalyzer()
