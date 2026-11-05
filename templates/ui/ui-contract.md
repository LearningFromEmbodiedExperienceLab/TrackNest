# Reusable UI kit

Copy this entire `ui/` directory into any project and load `ui.css`. No Flask, Jinja, application CSS, package installation, external fonts or build step is required. Serve `example.html` with a local static server to inspect every component in both themes.

```html
<!doctype html>
<html lang="en" data-theme="light">
<head><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="ui/ui.css"></head>
<body><!-- Host application content --></body>
</html>
```

The host application supplies content, translations and business interactions. The kit supplies visual primitives and native HTML patterns. Do not copy `static/app.css` as part of the kit.

## Design language

- Warm neutral colors, flat surfaces, thin borders and small 4/6/8px radii. Avoid gradients, heavy shadows and decorative cards around every paragraph.
- Use one page heading, clear section headings and short supporting text. Reserve monospace for identifiers and code; nicknames and normal copy use the system sans-serif stack.
- Use the 8/16/24/32/48px spacing scale. Default controls are 40px high and at least 44px on narrow screens; icon-only controls retain their compact 32px size. Pair icon controls with an accessible label.
- Prefer one primary action per task. Secondary and text actions recede; stop/delete/remove/leave actions use muted red, including on hover.
- Labels remain visible above fields. Put supporting information next to the relevant operation, not in a permanent page-wide banner.
- Use native `details` for occasional editing and long examples. Keep everyday operations and safety information visible. A collapsed section is not an authorization boundary.
- Tables may scroll within their wrapper; the page itself must not overflow. Stack application-specific rows on mobile only in the consuming project's stylesheet.

## Layers and theme contract

1. `styles/tokens.css`: fonts, sizes, spacing, radii, control height and duration.
2. `themes/light.css`, `themes/dark.css`: semantic colors only.
3. `styles/base.css`, `styles/components.css`: shared element defaults and `ui-*` components.
4. Host application: navigation, page widths, business layouts, permissions and translated text.

Switch `data-theme="light|dark"` on `<html>`; light is the default. The example includes a small optional theme toggle. Production persistence and translated accessibility labels belong to the host application.

Add another theme by importing its file from `ui.css` and defining every color listed in `tokens.css`. Override semantic tokens rather than hard-coding component colors. Existing class names remain supported.

## Actions and states

```html
<button class="ui-button" type="submit">Save</button>
<a class="ui-button ui-button--secondary" href="/projects">Projects</a>
<button class="ui-button ui-button--ghost" type="button">Cancel</button>
<button class="ui-button ui-button--danger" type="submit">Delete</button>
<button class="ui-button" type="button" disabled>Unavailable</button>
```

Links and buttons share dimensions and borders. Prefer native `disabled` on buttons. `aria-disabled="true"` only changes appearance; the host must also prevent activation for custom controls and links. Use `ui-icon-button` or `ui-theme-toggle` with `ui-icon` for labeled icon controls. Hover, visible keyboard focus and disabled states must remain distinguishable in both themes.

## Forms and layout primitives

```html
<form class="ui-stack">
  <label class="ui-field" for="display-name">Display name
    <input id="display-name" class="ui-input" name="nickname">
  </label>
  <label class="ui-field" for="role">Role
    <select id="role" class="ui-input" name="role"><option>Editor</option></select>
  </label>
  <label class="ui-field" for="notes">Notes
    <textarea id="notes" class="ui-textarea" rows="3"></textarea>
  </label>
  <div class="ui-form-footer"><p class="ui-hint">Optional supporting text.</p><button class="ui-button">Save</button></div>
</form>
```

Use `readonly` and `disabled` for native control states. Plain values that cannot be edited need text rather than an input. `ui-check` aligns a checkbox with a wrapping label. `ui-stack` provides consistent vertical gaps; `ui-actions` wraps related buttons. Page grids and form width constraints remain application-specific.

## Panels, tables and status

```html
<section class="ui-panel" aria-labelledby="list-heading">
  <div class="ui-pane-heading"><h2 id="list-heading">Workspaces</h2><span class="ui-pane-heading-meta">2 items</span></div>
  <div class="ui-table-wrap"><table class="ui-table">
    <thead><tr><th scope="col">Name</th><th scope="col">State</th></tr></thead>
    <tbody><tr><td>Example</td><td><span class="ui-status ui-status--active">Running</span></td></tr></tbody>
  </table></div>
  <div class="ui-panel-body"><p class="ui-hint">Supporting text.</p></div>
</section>
```

`ui-status` is neutral/inactive; `ui-status--active` fills its dot and emphasizes text. Always retain the status label; do not communicate state through color alone. `ui-notice` styles a status message; `ui-notice--danger` is an error variant. Add `role="status"` or `role="alert"` according to the behavior and urgency; appearance alone does not create a live region.

## Progressive disclosure and code

```html
<details class="ui-disclosure">
  <summary>Training example</summary>
  <div class="ui-disclosure-body ui-stack">
    <p class="ui-hint">Run this on your training machine.</p>
    <pre class="ui-code"><code>example.run()</code></pre>
  </div>
</details>
```

Native disclosures support mouse and keyboard without JavaScript. Escape untrusted code as normal HTML text. `ui-code` wraps long lines; applications decide whether sensitive values should be shown. Use `ui-prose` for longer documentation.

## Other supported components

`ui-muted`, `ui-mono`, `ui-hint`, `ui-kicker`, `ui-segment`, `ui-segment__btn`, `ui-segment__btn--active`, `ui-chip-group`, `ui-chip`, `ui-chip--active`, `ui-check`, `ui-icon`, `ui-icon-button`, `ui-theme-toggle`. Segment and chip interactions are supplied by the host; the example displays their visual states only.

## Reuse checklist

- Copy the entire directory, preserving relative imports, theme files and icon licenses.
- Test `example.html` without application assets before integrating.
- Check light/dark themes, keyboard focus, disabled/read-only states, long translated labels and 390px/mobile widths.
- Keep business selectors, names, routes and scripts outside this kit. Update this reference and the example when adding a shared component.
