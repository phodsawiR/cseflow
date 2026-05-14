import markdown
from weasyprint import HTML, CSS


def get_css() -> str:
    return """
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+Thai:wght@400;600;700&display=swap');

body {
  font-family: 'Noto Sans Thai', 'Sarabun', Arial, sans-serif;
  font-size: 13px;
  line-height: 1.7;
  margin: 2cm;
  color: #2c3e50;
}

h1 {
  font-size: 18px;
  border-bottom: 2px solid #2c3e50;
  padding-bottom: 6px;
  margin-top: 0;
}
h2 {
  font-size: 15px;
  border-bottom: 1px solid #ccc;
  color: #2c3e50;
  padding-bottom: 4px;
  margin-top: 20px;
  page-break-before: auto;
}
h3 { font-size: 13px; margin-top: 12px; }

table {
  border-collapse: collapse;
  width: 100%;
  margin: 10px 0;
  font-size: 12px;
}
td, th {
  border: 1px solid #ccc;
  padding: 6px 8px;
  text-align: left;
}
th { background: #f5f5f5; font-weight: bold; }

ul, ol { margin: 6px 0 6px 20px; }
li { margin-bottom: 3px; }
p { margin: 6px 0; }

code {
  background: #f5f5f5;
  padding: 1px 4px;
  border-radius: 3px;
  font-size: 12px;
}
pre {
  background: #f5f5f5;
  padding: 10px;
  border-radius: 4px;
  font-size: 12px;
  overflow-x: auto;
}
blockquote {
  border-left: 3px solid #ccc;
  margin-left: 0;
  padding-left: 10px;
  color: #555;
}

strong { font-weight: 700; }
"""


def md_to_pdf(md_text: str, output_path: str) -> bool:
    try:
        html_body = markdown.markdown(
            md_text,
            extensions=["tables", "fenced_code", "nl2br"],
        )
        html = f"""<!DOCTYPE html>
<html lang="th">
<head><meta charset="UTF-8"></head>
<body>{html_body}</body>
</html>"""
        HTML(string=html).write_pdf(
            output_path,
            stylesheets=[CSS(string=get_css())],
        )
        return True
    except Exception as e:
        print(f"[pdf_exporter] PDF export failed: {e}")
        return False
