# Kurukuru Frontend Review

Based on the design direction—blending a hyperscaler console's density, Nothing's restraint, and Google's legibility—the frontend is solid and adheres strictly to its design tokens. Below is an audit of areas where the implementation can be sharpened without reinventing the current UX. 

## 1. Interaction & Keyboard Affordances

**Missing Form Submissions in Modals**
* **File**: `frontend/src/components/ConfirmDialog.tsx`
* **Severity**: **Medium**
* **Finding**: The action buttons in the footer are not wrapped in a `<form>` element. Unlike `LaunchModal`, which binds the footer's submit button to a form using the `form` attribute, `ConfirmDialog` simply relies on `onClick`. Pressing `Enter` will not submit the dialog by default unless the button is explicitly focused. 

**Inaccessible Interactive Elements**
* **File**: `frontend/src/components/ActivityView.tsx`, `frontend/src/components/InstanceDetail.tsx`
* **Severity**: **High**
* **Finding**: Activity log rows use a standard `<div>` with an `onClick` handler to expand and show details. They lack a `tabIndex={0}`, `onKeyDown` handlers, and `role="button"`. Keyboard users cannot expand or interact with these logs.

## 2. Accessibility (Beyond Contrast)

**Semantically Hidden Progress Bars**
* **File**: `frontend/src/components/LaunchModal.tsx`
* **Severity**: **Medium**
* **Finding**: The `CapacityStrip` component communicates host resource allocation using styled `<span>` elements acting as a visual progress bar. It lacks the necessary ARIA attributes (`role="progressbar"`, `aria-valuenow`, `aria-valuemin`, `aria-valuemax`), meaning assistive technology will only read the raw text without interpreting the bar.

**Missing State on Accordions**
* **File**: `frontend/src/components/InstancesTable.tsx`
* **Severity**: **Medium**
* **Finding**: The "Show error details" chevron button has an `aria-label`, but does not include the `aria-expanded={expanded}` attribute. Screen reader users won't know if the error section is currently expanded or collapsed.

**Silent Copy State Changes**
* **File**: `frontend/src/components/InstanceDetail.tsx`
* **Severity**: **Low**
* **Finding**: The copy button for the SSH command visually changes to a green checkmark when clicked, but there is no `aria-live` region or dynamic `aria-label` change to announce "Copied" to screen readers. 

## 3. Responsive Behaviour

**Rigid Desktop-Only Layout**
* **Files**: `frontend/src/App.tsx`, `frontend/src/components/Sidebar.tsx`, `frontend/src/components/Header.tsx`
* **Severity**: **High**
* **Finding**: The application wrapper is strictly constrained with `flex h-screen overflow-hidden` and the sidebar is a fixed `w-56 shrink-0`. There are no media queries to collapse the sidebar into a hamburger menu or adjust the layout on small viewports. On mobile or tablet screens, the layout will aggressively squash the main content area, causing dense tables and the `Header.tsx` flexbox to overflow ungracefully.

## 4. Information Hierarchy

**Horizontal Crowding**
* **File**: `frontend/src/components/InstancesTable.tsx`
* **Severity**: **Low**
* **Finding**: The "Name" column aggregates the Instance name link, the `EngineBadge`, the Windows OS badge, and the ISO badge all on one horizontal flex row. On denser screens, this causes jagged wrapping. Stacking badges vertically underneath the name (or wrapping them as a secondary data line) would preserve density while drastically improving horizontal scanability.

## 5. Empty / Loading / Error States

**Missing Empty State for Network Forwards**
* **File**: `frontend/src/components/InstanceDetail.tsx`
* **Severity**: **Low**
* **Finding**: The `InstanceForwards` component simply renders an empty `<ul>` when there are no port forwards. Unlike "Volumes" and "Snapshots" which provide excellent explanatory copy when empty, the network section leaves the user staring at blank space. Adding an empty state here would improve consistency.
