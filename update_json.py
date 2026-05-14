import re

with open('session.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Replace source_plan with import json
content = re.sub(
    r'try:\s*import json\s*source_plan = json\.loads\(source_plan_raw\)\s*source = query_from_source_plan\(source_plan\)\s*except Exception:\s*source = source_plan_raw',
    r'''try:
                data = parse_llm_json(source_plan_raw)
                source_plan = SourcePlan(**data).model_dump()
                source = query_from_source_plan(source_plan)
            except Exception as e:
                print(f"[DEBUG] Source Plan parse error: {e}")
                source = source_plan_raw''',
    content
)

# Replace source_plan without import json
content = re.sub(
    r'try:\s*source_plan = json\.loads\(source_plan_raw\)\s*source = query_from_source_plan\(source_plan\)\s*except Exception:\s*source = source_plan_raw',
    r'''try:
                data = parse_llm_json(source_plan_raw)
                source_plan = SourcePlan(**data).model_dump()
                source = query_from_source_plan(source_plan)
            except Exception as e:
                print(f"[DEBUG] Source Plan parse error: {e}")
                source = source_plan_raw''',
    content
)

# Replace source_plan for Branch B (kb_result)
content = re.sub(
    r'try:\s*source_plan = json\.loads\(source_plan_raw\)\s*kb_result = query_from_source_plan\(source_plan\)\s*except Exception:\s*kb_result = source_plan_raw',
    r'''try:
             data = parse_llm_json(source_plan_raw)
             source_plan = SourcePlan(**data).model_dump()
             kb_result = query_from_source_plan(source_plan)
            except Exception as e:
             print(f"[DEBUG] Source Plan parse error: {e}")
             kb_result = source_plan_raw''',
    content
)

# Replace router_result (False)
content = re.sub(
    r'try:\s*router_result = json\.loads\(router_result_raw\)\s*except Exception:\s*router_result = \{"complexity": "medium", "spawn_professor": False\}',
    r'''try:
                data = parse_llm_json(router_result_raw)
                router_result = SmartRouterOutput(**data).model_dump()
            except Exception as e:
                print(f"[DEBUG] Smart Router parse error: {e}")
                router_result = {"complexity": "medium", "spawn_professor": False}''',
    content
)

# Replace router_result (True)
content = re.sub(
    r'try:\s*router_result = json\.loads\(router_result_raw\)\s*except Exception:\s*router_result = \{"complexity": "medium", "spawn_professor": True\}',
    r'''try:
                data = parse_llm_json(router_result_raw)
                router_result = SmartRouterOutput(**data).model_dump()
            except Exception as e:
                print(f"[DEBUG] Smart Router parse error: {e}")
                router_result = {"complexity": "medium", "spawn_professor": True}''',
    content
)

with open('session.py', 'w', encoding='utf-8') as f:
    f.write(content)
