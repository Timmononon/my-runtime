---
name: frontend-design
description: Design and implement a deliberate, accessible interface with responsive layout and browser evidence.
license: Apache-2.0; see LICENSE.txt and NOTICE.md
---

# Frontend design for Aster

Start from the product's audience, purpose and content. Choose an intentional visual direction: type hierarchy, density, spacing, color roles and one distinctive detail. Explain choices through the user's task, not decorative terminology. Preserve an existing design system when modifying a project.

1. Inspect the existing HTML/CSS and dependencies with file tools. Identify real entry points and constraints. Do not add an external font, analytics script or dependency without a task reason.
2. Use `templates/index.html` as a small standalone foundation when creating a new offline interface. It has semantic landmarks, keyboard focus, a responsive grid, CSS design tokens and reduced-motion support. Adapt its content and composition; the sample is not a finished design.
3. For a palette, `use_skill(theme-factory)` provides ten local theme references. Fonts named by a theme may not be installed on Windows; use local fallbacks or explicitly approved licensed assets.
4. Implement all promised interactions; show truthful empty/error/loading states. Prefer native controls and accessible labels. Avoid placeholder links, clipped text, color-only status, unexpected autoplay and animation that obscures content.
5. Present a local artifact with `present_deliverable` and test its returned preview URL through `browser_open`, `browser_snapshot` and `browser_screenshot`. Previews are static by default. JavaScript interaction tests require an explicit `present_deliverable(path=..., interactive=true)` request and normal tool approval; never automatically approve it. Interactive mode is not a complete network sandbox, including WebRTC/UDP. Check narrow and wide viewports if the browser tool supports them, keyboard navigation, focus, contrast and long content. Use `webapp-testing` for objective behavior assertions. An image alone cannot prove a button works.
6. Report the local output path and observed behavior, including any untested accessibility or backend dependency. Deployment and external publishing require a separate authorized action.
