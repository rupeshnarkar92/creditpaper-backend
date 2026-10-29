"""Templates module.

Defines one row per template (default + custom) per tenant, plus
per-template papers. The default template is auto-created when
onboarding completes; custom templates are user-created via the
Add Template wizard in the settings UI.
"""

from creditpaper.templates.models.template import Template, TemplatePaper

__all__ = ["Template", "TemplatePaper"]
