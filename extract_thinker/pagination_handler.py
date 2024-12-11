import copy
from typing import Any, Dict, List, Optional, get_origin
from pydantic import BaseModel
from instructor.exceptions import IncompleteOutputException
from extract_thinker.completion_handler import CompletionHandler
from extract_thinker.utils import encode_image, make_all_fields_optional
import yaml
from concurrent.futures import ThreadPoolExecutor, as_completed

class PaginationHandler(CompletionHandler):
    def __init__(self, llm):
        super().__init__(llm)
        
    def handle(self, 
               content: List[Dict[str, Any]],
               response_model: type[BaseModel],
               vision: bool = False,
               extra_content: Optional[str] = None) -> Any:
        # Make fields optional to allow partial results
        response_model_optional = make_all_fields_optional(response_model)
        
        # Process pages in parallel
        results = []
        with ThreadPoolExecutor() as executor:
            futures = []
            for page in content:
                # Build messages for each page
                messages = self._build_messages(page, vision)
                if extra_content:
                    self._add_extra_content(messages, extra_content)
                    
                # Submit page processing task
                future = executor.submit(
                    self._process_page, 
                    messages,
                    response_model_optional
                )
                futures.append(future)
            
            # Collect results as they complete
            for future in as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    # Log error but continue processing other pages
                    print(f"Error processing page: {str(e)}")
                    
        # Merge results from all pages
        if not results:
            raise ValueError("No valid results obtained from any page")
            
        return self._merge_results(results, response_model)

    def _process_page(self, messages: List[Dict[str, Any]], response_model: type[BaseModel]) -> Any:
        """Process a single page with retry logic for incomplete responses"""
        try:
            return self.llm.request(messages, response_model)
        except IncompleteOutputException as e:
            # Handle partial response
            partial_result = self._handle_partial_response(e, messages, response_model)
            
            # Check if we need to resolve any conflicts
            partial_dict = partial_result.model_dump()
            if self._has_conflicts(partial_dict, response_model):
                resolved_dict = self._resolve_conflicts(partial_dict, response_model)
                return response_model(**resolved_dict)
                
            return partial_result

    def _merge_results(self, results: List[Any], response_model: type[BaseModel]) -> Any:
        """Merge results from multiple pages into a dictionary, detect conflicts, resolve if needed, then return model."""
        
        # First, collect all values for each field from all results
        field_values = {}
        for result in results:
            result_dict = result.model_dump()
            for field_name, field_value in result_dict.items():
                if field_name not in field_values:
                    field_values[field_name] = []
                field_values[field_name].append(field_value)
        
        # Merge fields
        merged = {}
        for field_name, values in field_values.items():
            field_type = response_model.model_fields[field_name].annotation if field_name in response_model.model_fields else None
            non_null_values = [v for v in values if v is not None]

            if field_type and get_origin(field_type) is list:
                # Merge lists
                merged_list = []
                for v in values:
                    if isinstance(v, list):
                        merged_list.extend(v)
                    elif v is not None:
                        merged_list.append(v)
                # Check for duplicates if needed later
                merged[field_name] = merged_list
            else:
                # Scalar field handling
                if len(non_null_values) == 0:
                    merged[field_name] = None
                else:
                    distinct_values = list(set(non_null_values))
                    if len(distinct_values) == 1:
                        merged[field_name] = distinct_values[0]
                    else:
                        # Store conflicts in special structure
                        merged[field_name] = {
                            "_conflict": True,
                            "candidates": distinct_values
                        }

        # Check for conflicts and resolve if necessary
        if self._has_conflicts(merged, response_model):
            merged = self._resolve_conflicts(merged, response_model)
        
        # Now that conflicts are resolved, instantiate the response model
        return response_model(**merged)

    def _has_conflicts(self, result_dict: Dict[str, Any], response_model: type[BaseModel]) -> bool:
        """Check if result dictionary has any conflicting fields."""
        for field_name, field_value in result_dict.items():
            if field_name not in response_model.model_fields:
                continue
            field_type = response_model.model_fields[field_name].annotation
            
            # Check for special conflict dictionary (scalar conflict)
            if isinstance(field_value, dict) and field_value.get("_conflict"):
                return True
            
            # Check list field duplicates
            if field_type and get_origin(field_type) is list and isinstance(field_value, list):
                # If we detect duplicates for a list that might indicate a conflict
                # (e.g., multiple identical answers that should be distinct or need verification)
                if len(field_value) > 1:
                    # Check for real duplicates
                    if len(set(str(x) for x in field_value)) < len(field_value):
                        return True
        return False

    def _identify_conflicts(self, result_dict: Dict[str, Any], response_model: type[BaseModel]) -> Dict[str, Any]:
        """Identify conflicting fields in the result dictionary."""
        conflicts = {}
        for field_name, field_value in result_dict.items():
            if field_name not in response_model.model_fields:
                continue
            field_type = response_model.model_fields[field_name].annotation

            # Check scalar conflicts
            if isinstance(field_value, dict) and field_value.get("_conflict"):
                conflicts[field_name] = field_value["candidates"]
            # Check list conflicts (duplicates)
            elif field_type and get_origin(field_type) is list and isinstance(field_value, list):
                if len(field_value) > 1:
                    # If duplicates exist, consider it a conflict
                    if len(set(str(x) for x in field_value)) < len(field_value):
                        conflicts[field_name] = field_value
        return conflicts

    def _resolve_conflicts(self, result_dict: Dict[str, Any], response_model: type[BaseModel]) -> Dict[str, Any]:
        """Resolve conflicts in the dictionary using the LLM."""
        conflicts = self._identify_conflicts(result_dict, response_model)
        
        if not conflicts:
            return result_dict
            
        resolved = self._request_conflict_resolution(conflicts)
        return self._merge_resolved_conflicts(result_dict, resolved, response_model)

    def _request_conflict_resolution(
        self,
        conflicts: Dict[str, List[Any]]
    ) -> Dict[str, Dict[str, Any]]:
        """Request LLM to resolve conflicts."""
        prompt = self._build_conflict_resolution_prompt(conflicts)
        
        messages = [
            {
                "role": "system",
                "content": "You are a server API that resolves field conflicts."
            },
            {
                "role": "user",
                "content": prompt
            }
        ]
        
        try:
            response = self.llm.request(messages, dict)
            return response.get("resolved_fields", {})
        except Exception as e:
            raise ValueError(f"Failed to resolve conflicts: {str(e)}")

    def _build_conflict_resolution_prompt(self, conflicts: Dict[str, List[Any]]) -> str:
        """Build prompt for conflict resolution."""
        return (
            "Please resolve conflicts in these fields by choosing the correct value "
            "and providing a confidence score (1-10).\n\n"
            "Return JSON in this format:\n"
            "{\n"
            '  "resolved_fields": {\n'
            '    "field_name": {"value": "chosen_value", "confidence": 9}\n'
            "  }\n"
            "}\n\n"
            f"Conflicts to resolve:\n{conflicts}"
        )

    def _merge_resolved_conflicts(
        self,
        original: Dict[str, Any],
        resolved: Dict[str, Dict[str, Any]],
        response_model: type[BaseModel]
    ) -> Dict[str, Any]:
        """Merge resolved conflicts back into the dictionary."""
        result_dict = copy.deepcopy(original)
        
        for field_name, resolution in resolved.items():
            if field_name in result_dict:
                result_dict[field_name] = resolution["value"]
                
        return result_dict

    def _handle_partial_response(
        self,
        exception: IncompleteOutputException,
        messages: List[Dict[str, Any]],
        response_model: type[BaseModel]
    ) -> Any:
        """Handle partial response by continuing the request"""
        partial_content = exception.last_completion.choices[0].message.content
        continuation_messages = self._build_continuation_messages(messages, partial_content)
        
        try:
            return self.llm.request(continuation_messages, response_model)
        except Exception as e:
            raise ValueError(f"Failed to complete partial response: {str(e)}")
            
    def _build_continuation_messages(
        self,
        messages: List[Dict[str, Any]],
        partial_content: str
    ) -> List[Dict[str, Any]]:
        """Build messages for continuation request."""
        continuation_messages = copy.deepcopy(messages)
        
        # Add partial response as assistant message
        continuation_messages.append({
            "role": "assistant",
            "content": partial_content
        })
        
        # Add continuation prompt
        continuation_messages.append({
            "role": "user", 
            "content": "## CONTINUE JSON"
        })
        
        return continuation_messages

    def _build_messages(self, content: Any, vision: bool) -> List[Dict[str, Any]]:
        """Build messages for LLM request."""
        system_message = {
            "role": "system",
            "content": "You are a server API that receives document information and returns specific fields in JSON format."
        }
        
        if vision:
            message_content = self._build_vision_content(content)
            messages = [
                system_message,
                {
                    "role": "user",
                    "content": message_content
                }
            ]
        else:
            message_content = self._build_text_content(content)
            messages = [
                system_message,
                {
                    "role": "user",
                    "content": message_content
                }
            ]
            
        return messages
        
    def _build_vision_content(self, content: Any) -> List[Dict[str, Any]]:
        """Build content for vision request."""
        message_content = []
        
        # Add text content if available
        if isinstance(content, dict) and "content" in content:
            message_content.append({
                "type": "text",
                "text": f"##Content\n\n{content['content']}"
            })
            
        # Add images
        if isinstance(content, dict) and ("image" in content or "images" in content):
            images = content.get("images", [content.get("image")])
            for img in images:
                if img:
                    message_content.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{encode_image(img)}"
                        }
                    })
                    
        return message_content
        
    def _build_text_content(self, content: Any) -> str:
        """Build content for text request."""
        if isinstance(content, dict):
            return f"##Content\n\n{yaml.dump(content)}"
        elif isinstance(content, str):
            return f"##Content\n\n{content}"
        else:
            return f"##Content\n\n{str(content)}"
            
    def _add_extra_content(self, messages: List[Dict[str, Any]], extra_content: str) -> None:
        """Add extra content to messages."""
        messages.insert(1, {
            "role": "user",
            "content": f"##Extra Content\n\n{extra_content}"
        })