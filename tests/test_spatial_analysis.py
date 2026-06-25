import pytest
from app.models.schemas import ExtractedField, FieldPosition
from app.services.layout_analyzer import layout_analyzer

def create_field(text, x, y, width, height, page=0):
    return ExtractedField(
        fieldName="unknown",
        value=text,
        confidence=0.9,
        position=FieldPosition(x=x, y=y, width=width, height=height, page=page),
        rawText=text
    )

def test_find_value_near_label_right():
    # Scenario: Label "Name:" with value "John Doe" to its right
    label = create_field("Name:", 0.1, 0.1, 0.1, 0.05)
    value = create_field("John Doe", 0.25, 0.1, 0.2, 0.05) # Starting at 0.25, right of 0.2 (0.1+0.1)
    
    fields = [label, value]
    
    result = layout_analyzer.find_value_near_label(label, fields)
    assert result == value
    assert result.value == "John Doe"

def test_find_value_near_label_below():
    # Scenario: Label "Address:" with value "123 Main St" below it
    label = create_field("Address:", 0.1, 0.1, 0.2, 0.05)
    value = create_field("123 Main St", 0.1, 0.16, 0.5, 0.05) # y=0.16 is below 0.15
    
    fields = [label, value]
    
    result = layout_analyzer.find_value_near_label(label, fields)
    assert result == value

def test_prefer_right_over_below():
    # Scenario: Label has something to right AND below. Should prefer right usually.
    label = create_field("Label", 0.1, 0.1, 0.1, 0.05)
    right_val = create_field("RightValue", 0.22, 0.1, 0.1, 0.05)
    below_val = create_field("BelowValue", 0.1, 0.16, 0.1, 0.05)
    
    fields = [label, right_val, below_val]
    
    result = layout_analyzer.find_value_near_label(label, fields)
    assert result == right_val

def test_ignore_far_values():
    # Scenario: Value is too far away
    label = create_field("Label", 0.1, 0.1, 0.1, 0.05)
    far_val = create_field("FarValue", 0.8, 0.1, 0.1, 0.05) # Too far right
    
    fields = [label, far_val]
    
    result = layout_analyzer.find_value_near_label(label, fields)
    assert result is None
