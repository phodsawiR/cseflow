import os
import glob
import re
from datetime import datetime

vault_dir = r"C:\Users\USER\Obsidian vault"
date_str = datetime.now().strftime("%Y-%m-%d")

# Ignore special files
ignore_files = ["CLAUDE.md", "MOC.md", "Timeline Hub.md", "Untitled.base", "🏠 Ward Medicine Home.md"]

processed = 0
for root, dirs, files in os.walk(vault_dir):
    # skip hidden directories
    dirs[:] = [d for d in dirs if not d.startswith('.')]
    
    for file in files:
        if not file.endswith(".md") or file in ignore_files:
            continue
            
        file_path = os.path.join(root, file)
        
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        # Check if already has YAML
        if content.startswith("---\n") or content.startswith("---\r\n"):
            continue
            
        # Extract tags
        # Tags in these generated files are usually on a single line starting with #
        # e.g. "#disease #cardiology"
        # We find lines that consist ONLY of tags (and whitespace) to avoid matching markdown headers
        # Wait, markdown headers have a space after # like "# Heading". Tags have no space.
        
        lines = content.split('\n')
        new_lines = []
        tags = []
        
        # Regex for valid Obsidian tags: starts with #, then letters/numbers/hyphens/underscores
        tag_pattern = re.compile(r'#([A-Za-z0-9_-]+)')
        
        for line in lines:
            # If line starts with '#' but NOT followed by space, it's likely our tag line
            if line.startswith('#') and len(line) > 1 and line[1] != ' ':
                # Extract all tags on this line
                found_tags = tag_pattern.findall(line)
                if found_tags:
                    tags.extend(found_tags)
                continue # Skip adding this line to the new content
            new_lines.append(line)
            
        # Limit to 4 tags
        tags = tags[:4]
        
        # Construct YAML
        yaml_lines = [
            "---",
            f"tags: [{', '.join(tags)}]",
            "---",
            f"[[{date_str}]]",
            ""
        ]
        
        # Combine
        final_content = "\n".join(yaml_lines) + "\n".join(new_lines)
        
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(final_content)
            
        processed += 1

print(f"Migration complete! Processed {processed} files.")