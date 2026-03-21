"""Built-in action types.

Registry maps action type string to Python class.
Adding a new action: create a file, add it to ACTION_TYPES below.
"""

from evalforge_runtime.actions.builtins.email_reply import EmailReplyAction
from evalforge_runtime.actions.builtins.email_forward import EmailForwardAction
from evalforge_runtime.actions.builtins.email_move import EmailMoveAction
from evalforge_runtime.actions.builtins.email_mark_read import EmailMarkReadAction
from evalforge_runtime.actions.builtins.email_send import EmailSendAction
from evalforge_runtime.actions.builtins.webhook_post import WebhookPostAction
from evalforge_runtime.actions.builtins.file_save_output import FileSaveOutputAction
from evalforge_runtime.actions.builtins.gdrive_upload import GDriveUploadAction
from evalforge_runtime.actions.builtins.sharepoint_upload import SharePointUploadAction
from evalforge_runtime.actions.builtins.process_call import ProcessCallAction

# Type string → action class mapping. Used by ActionRunner/builder to resolve types.
ACTION_TYPES = {
    "email.reply": EmailReplyAction,
    "email.forward": EmailForwardAction,
    "email.move": EmailMoveAction,
    "email.mark_read": EmailMarkReadAction,
    "email.send": EmailSendAction,
    "webhook.post": WebhookPostAction,
    "file.save_output": FileSaveOutputAction,
    "gdrive.upload": GDriveUploadAction,
    "sharepoint.upload": SharePointUploadAction,
    "process.call": ProcessCallAction,
}

__all__ = [
    "ACTION_TYPES",
    "EmailReplyAction",
    "EmailForwardAction",
    "EmailMoveAction",
    "EmailMarkReadAction",
    "EmailSendAction",
    "WebhookPostAction",
    "FileSaveOutputAction",
    "GDriveUploadAction",
    "SharePointUploadAction",
    "ProcessCallAction",
]
