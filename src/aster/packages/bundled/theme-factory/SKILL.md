---
name: theme-factory
description: Apply one of ten local theme palettes and typography references consistently to an HTML artifact.
license: Apache-2.0; see LICENSE.txt and NOTICE.md
---

# Theme factory

This package preserves ten Apache-2.0 theme references from the upstream theme-factory. It applies colors and typography to Aster's HTML/Markdown artifacts; it does not provide a slide or Office layout engine.

1. Read `themes/<name>.md` through `use_skill` reference loading. Available names: arctic-frost, botanical-garden, desert-rose, forest-canopy, golden-hour, midnight-galaxy, modern-minimalist, ocean-depths, sunset-boulevard, tech-innovation.
2. Match the audience and tone, and retain an existing brand if provided. If the user specified a theme, use it. Otherwise choose a fitting theme and state the choice; ask only when the brand decision is consequential.
3. Map its palette to semantic CSS variables: background, surface, text, muted text, accent and border. Check the actual text/background contrast; a palette does not guarantee every pairing is accessible. Use the specified fonts only when locally available and licensed; provide Segoe UI/Arial/Georgia fallbacks for Windows.
4. Apply the system consistently to headings, links, buttons, tables and charts. Preserve information and interaction behavior. Avoid inventing numerical chart values for decoration.
5. Generate a local palette preview with `scripts/showcase.py --output PATH` through the managed `process_exec` tool. It embeds all palettes in a standalone HTML file and refuses to overwrite an existing file. Open the result with the scoped browser preview tools, then inspect the final artifact at realistic sizes.

The original binary PDF showcase is intentionally omitted; the HTML showcase is generated locally without downloads or an Office dependency.
