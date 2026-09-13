---
version: alpha
name: "IoT Box Restaurant Control"
description: "Warm, high-density local operations console for restaurant hardware and precise receipt printing."
colors:
  background: "#F6EFE6"
  surface: "#FFFAF4"
  text: "#2F2318"
  muted: "#77604C"
  primary: "#A24C28"
  danger: "#A3342A"
  success: "#246A45"
  warning: "#8C6116"
typography:
  sans:
    fontFamily: "IBM Plex Sans, Segoe UI, sans-serif"
  display:
    fontFamily: "IBM Plex Sans, Segoe UI, sans-serif"
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
rounded:
  sm: "0.75rem"
  DEFAULT: "0.875rem"
  lg: "1.5rem"
spacing:
  control-gap: "0.625rem"
  panel-gap: "1.125rem"
  page-max: "73.75rem"
components:
  button: {}
  field: {}
  panel: {}
  receipt-paper: {}
---

# IoT Box Restaurant Control Design System

## Overview

The creative North Star is a well-kept restaurant service counter: warm paper, dark ink, compact controls, and measurements that are visible at the moment they matter. This is a product surface for restaurant operators configuring local hardware, often under time pressure on a laptop or tablet. The interface is Chinese-first while retaining protocol and product names such as RAW, ESC/POS, Font A, and Font B where translation would reduce precision.

The memorable signature is the receipt ruler: a dark, numbered measurement strip immediately above the paper preview. It makes physical output constraints visible without turning the rest of the console into decoration. Forms, confirmations, and device status remain restrained and familiar. Avoid generic blue SaaS dashboards, decorative glass effects, and visual motifs that compete with operational state.

Runtime CSS variables in `web/styles.css` are canonical; this document mirrors their roles and values. Durable token changes must update both files together.

## Colors

Warm cream is the application ground and paper-adjacent surfaces stay nearly white. Brown is the primary text and brand family, with separate green, amber, and red semantic tones. Focus uses a translucent primary ring. Status meaning is always paired with text, never color alone. Forced-colors mode yields scrollbar control back to the system.

## Typography

IBM Plex Sans is the operational text and heading face, using weight and size rather than a second family for hierarchy. Monospace is used only for receipt output, measurements, identifiers, and diagnostics. Chinese-capable system fallbacks are permitted. Control labels remain sentence case; protocol acronyms retain their canonical casing.

## Layout

The console uses a centered 1180px maximum canvas. Primary controls keep 42–48px minimum heights; expert configuration panels may use compact 34px controls arranged as horizontal label/value cells and remain collapsed until needed. The receipt studio uses block list, inspector, and preview columns on wide screens and stacks them below 980px. Scroll ownership belongs to the paper and diagnostics surfaces, not the whole editor. Async messages reserve a line so controls do not jump.

## Elevation & Depth

Hierarchy comes primarily from surface tone and one border. Wide panels may use the existing soft downward shadow; nested tool surfaces stay flat. The physical paper preview is the strongest elevated object because it represents the final artifact.

## Shapes

Controls use 12–14px radii, panels 18–24px, and compact status chips may be pills. Receipt paper and ruler use crisp edges to reinforce measurement accuracy.

## Components

Buttons expose hover, focus, pressed, disabled, and busy states without changing dimensions. Primary actions save or print; destructive red is reserved for deletion and confirmed replacement. Native selects are canonical because platform popup geometry is acceptable for this local console.

The receipt block list supports both drag-and-drop and explicit up/down controls. Profile and editor forms use persistent labels. Validation and overflow problems appear inline, name the affected line, and prevent unsafe save or print. Real print actions use an app-owned confirmation dialog and report queue submission or failure in a live status region.

Motion is short and functional; reduced-motion removes nonessential transitions. Product copy names the operation and recovery in Chinese.

## Do's and Don'ts

- **Do:** Show physical units, effective columns, and clipping risk beside the preview.
- **Do:** Keep preview, diagnostics, and RAW output derived from the same printer Profile.
- **Don't:** Use abstract size names when exact ESC/POS multipliers are available.
- **Don't:** hide print errors, depend on drag alone, or imply that a browser preview proves physical calibration.
