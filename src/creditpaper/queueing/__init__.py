"""Local-first task delivery; PostgreSQL remains the workflow authority."""

from creditpaper.queueing.dispatch import dispatch_job

__all__ = ["dispatch_job"]
