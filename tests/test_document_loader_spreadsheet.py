import os
import pytest
from io import BytesIO
from pydantic import BaseModel
from extract_thinker.document_loader.document_loader_spreadsheet import DocumentLoaderSpreadSheet
from extract_thinker.extractor import Extractor
from extract_thinker.llm import LLM


class TestSpreadsheetData(BaseModel):
    """Test model for spreadsheet data extraction"""
    sheet_names: list[str]
    total_rows: int
    sample_value: str


class TestDocumentLoaderSpreadSheet:
    @pytest.fixture
    def loader(self):
        """Create a spreadsheet document loader instance"""
        return DocumentLoaderSpreadSheet()

    @pytest.fixture
    def test_file_path(self):
        """Path to test Excel file"""
        current_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(current_dir, 'files', 'test_spreadsheet.xlsx')

    @pytest.fixture
    def mock_llm(self, monkeypatch):
        """Mock LLM that returns predefined responses based on input"""
        class MockLLM(LLM):
            def __init__(self, *args, **kwargs):
                self.model = "mock_model"
                self.page_count = 1
                self.thinking = False
                self.backend = None
                
            def request(self, messages, response_model):
                """Mock response based on content in messages"""
                # Get the content from the message
                if len(messages) > 1 and "content" in messages[1]:
                    content = messages[1]["content"]
                    
                    # Debugging - print what's being received
                    print(f"Mock LLM received: {content[:50]}...")
                    
                    # Check if this is spreadsheet data (after our fix)
                    if "Sheet:" in content or "Sheet1" in content or "test_value" in content:
                        # Find a sample value from the spreadsheet content
                        sample_value = "No sample"
                        if "test_value" in content:
                            sample_value = "test_value"
                        
                        # Count sheets
                        sheet_names = []
                        lines = content.split("\n")
                        for line in lines:
                            if line.startswith("Sheet:"):
                                sheet_name = line.replace("Sheet:", "").strip()
                                sheet_names.append(sheet_name)
                            elif "Sheet1" in line or "Sheet2" in line:
                                # Alternative detection method
                                if "Sheet1" in line and "Sheet1" not in sheet_names:
                                    sheet_names.append("Sheet1")
                                if "Sheet2" in line and "Sheet2" not in sheet_names:
                                    sheet_names.append("Sheet2")
                        
                        # If still no sheets found but we have test_value, 
                        # assume Sheet1 exists
                        if not sheet_names and "test_value" in content:
                            sheet_names = ["Sheet1"]
                        
                        # Count rows (simplified)
                        total_rows = content.count("\n")
                        
                        return TestSpreadsheetData(
                            sheet_names=sheet_names,
                            total_rows=total_rows,
                            sample_value=sample_value
                        )
                
                # Default response
                return TestSpreadsheetData(
                    sheet_names=[],
                    total_rows=0, 
                    sample_value="No data found"
                )
                
            def set_thinking(self, *args, **kwargs):
                pass
                
            def set_page_count(self, *args, **kwargs):
                pass
        
        # Return the mock LLM
        return MockLLM()

    def test_can_handle(self, loader, test_file_path):
        """Test that DocumentLoaderSpreadSheet can handle supported file types"""
        # Test with supported file types
        assert loader.can_handle(test_file_path) == True
        
        # Test with unsupported file types
        assert loader.can_handle("test.pdf") == False
        assert loader.can_handle("test.txt") == False

    def test_load_spreadsheet(self, loader, test_file_path):
        """Test loading content from a spreadsheet file"""
        # Load the spreadsheet
        pages = loader.load(test_file_path)
        
        # Assertions
        assert isinstance(pages, list)
        assert len(pages) > 0
        
        # Check page structure
        page = pages[0]
        assert "content" in page
        assert "data" in page
        assert "is_spreadsheet" in page
        assert "sheet_name" in page
        
        # Verify flags
        assert page["is_spreadsheet"] == True
        assert isinstance(page["data"], list)
        assert page["content"].startswith("Sheet:")

    def test_vision_mode_not_supported(self, loader):
        """Test that vision mode is not supported for spreadsheets"""
        assert loader.can_handle_vision("test.xlsx") == False
        
        # Setting vision mode should not change behavior for spreadsheets
        loader.set_vision_mode(True)
        assert loader.vision_mode == True  # Setting is allowed
        assert loader.can_handle_vision("test.xlsx") == False  # But still not supported

    def test_integration_with_extractor(self, loader, test_file_path, mock_llm):
        """Test the integration with Extractor to ensure spreadsheet data is correctly passed to LLM"""
        # Create an extractor with our mock LLM
        extractor = Extractor(document_loader=loader, llm=mock_llm)
        
        # Extract data
        result = extractor.extract(test_file_path, TestSpreadsheetData)
        
        # Verify the extraction result
        assert isinstance(result, TestSpreadsheetData)
        assert len(result.sheet_names) > 0
        assert "Sheet1" in result.sheet_names
        assert result.total_rows > 0
        
        # The sample value check will depend on your test spreadsheet content
        assert result.sample_value != "No data found"
        
    def test_in_memory_spreadsheet(self, loader, test_file_path, mock_llm):
        """Test loading from an in-memory BytesIO object"""
        # Read the test file into memory
        with open(test_file_path, "rb") as f:
            file_content = f.read()
        
        # Create a BytesIO object
        bio = BytesIO(file_content)
        
        # Load from BytesIO
        pages = loader.load(bio)
        
        # Verify structure
        assert isinstance(pages, list)
        assert len(pages) > 0
        assert pages[0]["is_spreadsheet"] == True
        
        # Test with extractor
        extractor = Extractor(document_loader=loader, llm=mock_llm)
        result = extractor.extract(bio, TestSpreadsheetData)
        
        # Verify results
        assert isinstance(result, TestSpreadsheetData)
        assert len(result.sheet_names) > 0
        
    def test_convert_to_image(self, loader, test_file_path):
        """Test converting spreadsheet to images"""
        try:
            # This test requires matplotlib, which might not be installed
            import matplotlib
            import pandas
            
            # Convert to image
            images = loader.convert_to_image(test_file_path)
            
            # Verify results
            assert images is not None
            assert isinstance(images, dict)
            assert len(images) > 0
            assert "Sheet1" in images
            assert "Sheet2" in images
            
            # Check image format
            assert isinstance(images["Sheet1"], bytes)
            # PNG images start with these bytes
            assert images["Sheet1"].startswith(b'\x89PNG')
        except ImportError:
            pytest.skip("Skipping image conversion test - matplotlib or pandas not installed")
    
    def test_convert_to_pdf(self, loader, test_file_path):
        """Test converting spreadsheet to PDF"""
        try:
            # This test requires fpdf, which might not be installed
            import fpdf
            import pandas
            
            # Convert to PDF
            pdf_data = loader.convert_to_pdf(test_file_path)
            
            # Verify results
            assert pdf_data is not None
            assert isinstance(pdf_data, BytesIO)
            
            # PDF files start with %PDF
            pdf_bytes = pdf_data.getvalue()
            assert pdf_bytes.startswith(b'%PDF')
        except ImportError:
            pytest.skip("Skipping PDF conversion test - fpdf or pandas not installed") 