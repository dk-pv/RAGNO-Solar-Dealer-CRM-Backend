"""The documents every Work needs, and how complete a Work's are.

The backend owns this list: the screens read it from the API (GET /api/works/{id}/documents/, and each Work's
document_summary), so it is defined once, here. Keys are stable identifiers, stored with each upload and used in URLs;
the names are only for display.

Every Work goes through the Loan Work / Documents and the Subsidy Documentation stages, so every Work needs both
groups. Email ID and Phone number are the Work's own customer fields: they are checked there, never uploaded as files.
"""

import os
from dataclasses import dataclass

from rest_framework.exceptions import NotFound, ValidationError


@dataclass(frozen=True)
class Requirement:
    key: str
    name: str
    group: str
    # Information the Work itself holds (its 'email' or 'phone' field), rather than a file to upload.
    field: str = ''
    required: bool = True


GROUPS = [('LOAN', 'Customer / loan documents'), ('SUBSIDY', 'Subsidy documents')]

# In the order the Documents page lists them.
REQUIREMENTS = [
    Requirement('bank_passbook', 'Bank passbook', 'LOAN'),
    Requirement('aadhar_card', 'Aadhaar card', 'LOAN'),
    Requirement('pan_card', 'PAN card', 'LOAN'),
    Requirement('electricity_bill', 'Electricity bill', 'LOAN'),
    Requirement('email_id', 'Email ID', 'LOAN', field='email'),
    Requirement('phone_number', 'Phone number', 'LOAN', field='phone'),
    Requirement('site_photo', 'Site photo', 'LOAN'),
    Requirement('land_tax_paper', 'Land tax paper', 'LOAN'),
    Requirement('name_signature', 'Name & signature (white paper)', 'LOAN'),
    Requirement('dcr', 'DCR', 'SUBSIDY'),
    Requirement('gps_photo', 'GPS photo', 'SUBSIDY'),
    Requirement('inverter_serial_number', 'Inverter serial number', 'SUBSIDY'),
]

# The documents uploaded as files: the choices of WorkDocument.document_key.
FILE_DOCUMENTS = [(requirement.key, requirement.name) for requirement in REQUIREMENTS if not requirement.field]

# What a document file may be. The type is recognised from how the file's contents start, never from the type the
# browser claims, and the file's name must end in one of that type's extensions.
FILE_TYPES = [
    ('application/pdf', ('.pdf',), lambda head: head.startswith(b'%PDF-')),
    ('image/jpeg', ('.jpg', '.jpeg'), lambda head: head.startswith(b'\xff\xd8\xff')),
    ('image/png', ('.png',), lambda head: head.startswith(b'\x89PNG\r\n\x1a\n')),
    ('image/webp', ('.webp',), lambda head: head[:4] == b'RIFF' and head[8:12] == b'WEBP'),
]
ACCEPTED_EXTENSIONS = [extension for _, extensions, _ in FILE_TYPES for extension in extensions]
# Cloudinary's free plan stores files of up to 10 MB.
MAX_FILE_SIZE = 10 * 1024 * 1024

WRONG_TYPE = 'Upload a PDF, JPG, PNG or WebP file.'
TOO_LARGE = f'The file is larger than {MAX_FILE_SIZE // (1024 * 1024)} MB.'


def is_provided(requirement, work, uploaded_keys):
    if requirement.field:
        return bool(getattr(work, requirement.field).strip())
    return requirement.key in uploaded_keys


def summarize(work):
    """How complete the Work's documents are. Reads work.documents.all(), which the Works API prefetches for a whole
    page of Works in one query."""
    uploaded = {document.document_key for document in work.documents.all()}
    required = [requirement for requirement in REQUIREMENTS if requirement.required]
    missing = [requirement.name for requirement in required if not is_provided(requirement, work, uploaded)]
    return {
        'required_count': len(required),
        'completed_count': len(required) - len(missing),
        'missing_count': len(missing),
        'is_complete': not missing,
        'missing_documents': missing,
    }


def file_requirement(key):
    """The document uploaded as a file under `key`. Any other key, Email ID and Phone number included, is not found."""
    requirement = next((requirement for requirement in REQUIREMENTS if requirement.key == key), None)
    if requirement is None or requirement.field:
        raise NotFound('There is no such document.')
    return requirement


def check_file(upload):
    """The uploaded file's content type, read from its contents, and its name's extension, which must match. Refuses
    anything else with a message for the screen. (Django has already cleaned the name: no path, no unprintable
    characters, at most 255 characters.)"""
    if upload is None:
        raise ValidationError({'file': ['Choose a file to upload.']})
    if upload.size == 0:
        raise ValidationError({'file': ['The file is empty.']})
    if upload.size > MAX_FILE_SIZE:
        raise ValidationError({'file': [TOO_LARGE]})
    head = upload.read(16)
    upload.seek(0)
    extension = os.path.splitext(upload.name)[1].lower()
    for content_type, extensions, starts_like in FILE_TYPES:
        if starts_like(head):
            if extension not in extensions:
                raise ValidationError({'file': [f"The file's name doesn't match its contents. {WRONG_TYPE}"]})
            return content_type, extension
    raise ValidationError({'file': [WRONG_TYPE]})
