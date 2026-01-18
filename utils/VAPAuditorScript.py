import os
import json
import hashlib
import re
from pathlib import Path

"""
VAP Auditor v1.1 - Core Tool for Context Reduction and Layer Integrity.
Part of the Vibe Architecture Protocol (VAP).
"""

EXCLUDE_DIRS = {'.git', 'node_modules', '__pycache__', 'dist', 'build', '.venv', '.next'}
SUPPORTED_EXTENSIONS = {'.py', '.js', '.ts', '.jsx', '.tsx', '.go', '.java'}

# Define Layer Violation Rules
LAYER_RULES = {
    "api": ["SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM", "db.", "prisma.", "repository"],
    "routes": ["SELECT ", "INSERT INTO", "db.", "prisma.", "sql"],
    "views": ["db.", "axios.", "fetch(", "api."]
}

class VAPAuditor:
    def __init__(self, root_dir):
        self.root_dir = Path(root_dir)
        self.index = {
            "protocol": "VAP v1.1",
            "stats": {"total_files": 0, "duplicates_found": 0, "violations_found": 0},
            "modules": [],
            "violations": []
        }

    def get_fingerprint(self, code):
        """Create a hash of code stripped of whitespace to find logical duplicates."""
        clean_code = "".join(code.split())
        return hashlib.md5(clean_code.encode()).hexdigest()

    def check_layer_violations(self, relative_path, content):
        path_str = str(relative_path).lower()
        violations = []
        for folder, forbidden_words in LAYER_RULES.items():
            if folder in path_str:
                for word in forbidden_words:
                    if word.lower() in content.lower():
                        violations.append(f"Layer Violation: '{word}' found in '{folder}' folder.")
        return violations

    def scan(self):
        fingerprints = {}
        for root, dirs, files in os.walk(self.root_dir):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for file in files:
                if any(file.endswith(ext) for ext in SUPPORTED_EXTENSIONS):
                    path = Path(root) / file
                    relative_path = path.relative_to(self.root_dir)
                    try:
                        with open(path, 'r', encoding='utf-8') as f:
                            content = f.read()
                            fp = self.get_fingerprint(content)
                            
                            is_duplicate = fp in fingerprints
                            if is_duplicate: self.index["stats"]["duplicates_found"] += 1
                            else: fingerprints[fp] = relative_path

                            violations = self.check_layer_violations(relative_path, content)
                            if violations:
                                self.index["stats"]["violations_found"] += len(violations)
                                self.index["violations"].append({"file": str(relative_path), "issues": violations})

                            symbols = re.findall(r'(?:function|class|def|const)\s+([a-zA-Z_][a-zA-Z0-9_]*)', content)
                            self.index["modules"].append({
                                "file": str(relative_path),
                                "symbols": list(set(symbols[:15])),
                                "hash": fp,
                                "is_duplicate": is_duplicate
                            })
                            self.index["stats"]["total_files"] += 1
                    except Exception: pass

    def save_index(self):
        with open('VAP_INDEX.json', 'w') as f:
            json.dump(self.index, f, indent=4)
        print(f"✅ VAP_INDEX generated. Files: {self.index['stats']['total_files']}, Duplicates: {self.index['stats']['duplicates_found']}, Violations: {self.index['stats']['violations_found']}")

if __name__ == "__main__":
    auditor = VAPAuditor('.')
    auditor.scan()
    auditor.save_index()