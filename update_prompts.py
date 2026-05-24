import os
import glob

prompts_dir = r"C:\Users\USER\OneDrive\Desktop\caseflow\prompts"
for file_path in glob.glob(os.path.join(prompts_dir, "formatter*.md")):
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    new_lines = []
    inserted = False
    for line in lines:
        new_lines.append(line)
        if ("## Rules" in line or "## Formatting Rules" in line) and not inserted:
            new_lines.append("- CRITICAL: บรรทัดแรกสุดของไฟล์ต้องเป็น YAML Frontmatter: `---\ntags: [tag1, tag2]\n---` (ไม่เกิน 4 tags)\n")
            new_lines.append("- CRITICAL: บรรทัดแรกสุดของเนื้อหาหลัง YAML ต้องใส่วันที่ในรูปแบบ `[[YYYY-MM-DD]]` เสมอ\n")
            inserted = True
            
    with open(file_path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    print(f"Updated {os.path.basename(file_path)}")