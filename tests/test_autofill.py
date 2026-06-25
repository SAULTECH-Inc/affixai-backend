import pytest
from app.services.autofill_service import AutoFillService
from app.models.schemas import ExtractedField, FieldPosition

def create_field(text, x, y, width, height):
    return ExtractedField(
        fieldName="unknown",
        value=text,
        confidence=0.9,
        position=FieldPosition(x=x, y=y, width=width, height=height, page=0),
        rawText=text
    )

def test_estimate_field_position():
    service = AutoFillService()
    
    # Mock fields with a "Signature" label
    label = create_field("Signature:", 0.1, 0.5, 0.2, 0.05)
    fields = [label]
    
    # Estimate position for "signature"
    # Should find the label and return a position below or right
    pos = service._estimate_field_position("signature", fields, {})
    
    assert pos is not None
    assert pos['x'] >= 0.1
    assert pos['y'] > 0.5 # Should be below or close
    
def test_synonym_matching():
    service = AutoFillService()
    
    match = service._find_matching_user_data(
        "Surname", "Doe", 
        {"lastName": "Doe"}, 
    )
    # Surname should match lastName
    assert match is not None
    assert match[0] == "lastName"
