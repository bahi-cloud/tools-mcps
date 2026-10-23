# 
# أداة لتوليد tools_config.json من TOOLS في tools.py

import json
import sys
from pathlib import Path
from dataclasses import asdict

# إضافة مسار المشروع
sys.path.insert(0, str(Path(__file__).parent.parent))

import tools

def generate_manifest():
    """توليد tools_config.json من TOOLS في tools.py"""
    config = {
        "version": 1,
        "tools": {},
        "categories": {}
    }
    
    for name, tool in tools.TOOLS.items():
        config["tools"][name] = {
            "description": tool.description,
            "parameters": tool.parameters,
            "category": tool.category,
            "enabled": tool.enabled,
            "hidden": tool.hidden,
            "aliases": tool.aliases,
            "tags": tool.tags
        }
        
        if tool.category not in config["categories"]:
            config["categories"][tool.category] = {
                "icon": "📦",
                "description": f"أدوات {tool.category}"
            }
    
    output_path = Path(__file__).parent.parent / "tools_config.json"
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
    
    print(f"✅ Generated tools_config.json at {output_path}")
    print(f"📊 Total tools: {len(config['tools'])}")
    print(f"📂 Categories: {list(config['categories'].keys())}")

if __name__ == "__main__":
    generate_manifest()