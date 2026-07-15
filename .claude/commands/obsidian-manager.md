Write AI-generated content to the Obsidian Vault. Use when saving research results, analysis, or generated content to Obsidian.

## Constraints (MANDATORY)

1. **Write-only, sandbox only**: Never modify existing notes. The ONLY permitted output path is:
   - `D:\マイドライブ\Obsidian\01_Daily\` (Daily notes — preferred)
   - Or any path explicitly specified by the user

2. **Format**: Always `.md` (Markdown) with YAML Frontmatter

3. **Encoding**: Always UTF-8

## Required Frontmatter

All AI-generated files MUST start with:

```yaml
---
aliases: []
tags:
  - 🤖AI_Generated
  - Antigravity
created: YYYY-MM-DDTHH:mm:ss
---
```

Replace `created` with the actual current timestamp in ISO 8601 format.

## Wikilinks

When writing content, aggressively link relevant keywords as `[[keyword]]` to support Obsidian knowledge graph building. Link project names, people, tools, concepts, and any cross-referenced topics.

## Python Implementation (Preferred)

```python
# -*- coding: utf-8 -*-
from pathlib import Path
from datetime import datetime

vault_inbox = Path(r"D:\マイドライブ\Obsidian\01_Daily")
vault_inbox.mkdir(parents=True, exist_ok=True)

timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
filename = f"AI_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"

frontmatter = f"""---
aliases: []
tags:
  - 🤖AI_Generated
  - Antigravity
created: {timestamp}
---

"""

content = frontmatter + your_content_here

with open(vault_inbox / filename, "w", encoding="utf-8") as f:
    f.write(content)
```

## PowerShell Alternative

```powershell
# Ensure directory exists
New-Item -ItemType Directory -Force -Path "D:\マイドライブ\Obsidian\01_Daily"

# Write file (UTF8 explicit)
Set-Content -Path "D:\マイドライブ\Obsidian\01_Daily\AI_20260228.md" -Value $Content -Encoding UTF8
```

## Naming Convention

- Daily notes: `YYYY-MM-DD.md` (existing file, append if exists)
- AI-generated standalone: `AI_YYYYMMDD_HHMMSS_topic.md`
- Research reports: `Research_YYYYMMDD_topic.md`
