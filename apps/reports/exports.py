"""Report exports as CSV or Excel (.xlsx). The .xlsx is written with the standard library: the project has no spreadsheet
package, and one sheet of plain values needs none."""

import csv
import io
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal
from xml.sax.saxutils import escape

from django.http import HttpResponse
from django.utils import timezone

from apps.leads.views import csv_cell

XLSX_TYPE = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
# Characters XML 1.0 can't carry.
CONTROL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')


def text(value):
    """A value as the report shows it: times in the CRM's time zone (Asia/Kolkata)."""
    if value is None:
        return ''
    if isinstance(value, datetime):
        return timezone.localtime(value).strftime('%Y-%m-%d %H:%M:%S')
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def export(fmt, name, header, rows):
    """The rows as a download. `rows` may be a generator, so a large CSV is written without holding every row twice."""
    filename = f'{name}-{timezone.localdate():%Y-%m-%d}.{fmt}'
    if fmt == 'csv':
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        response.write('﻿')  # Lets Excel read the file as UTF-8.
        writer = csv.writer(response)
        writer.writerow(header)
        for row in rows:
            writer.writerow([csv_cell(text(value)) for value in row])
    else:
        response = HttpResponse(xlsx(header, rows), content_type=XLSX_TYPE)
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


def column_name(index):
    """0 -> A, 25 -> Z, 26 -> AA."""
    letters = ''
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def xlsx_cell(ref, value):
    # Numbers stay numbers, so Excel can add them up. Everything else is text, never a formula.
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(CONTROL.sub("", text(value)))}</t></is></c>'


def xlsx(header, rows):
    sheet_rows = []
    for number, row in enumerate([header, *rows], start=1):
        cells = ''.join(xlsx_cell(f'{column_name(index)}{number}', value) for index, value in enumerate(row))
        sheet_rows.append(f'<row r="{number}">{cells}</row>')
    main = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    relationships = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    package = 'http://schemas.openxmlformats.org/package/2006/relationships'
    files = {
        '[Content_Types].xml': (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '</Types>'
        ),
        '_rels/.rels': (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{package}">'
            f'<Relationship Id="rId1" Type="{relationships}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        ),
        'xl/workbook.xml': (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{main}" xmlns:r="{relationships}">'
            '<sheets><sheet name="Report" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        'xl/_rels/workbook.xml.rels': (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{package}">'
            f'<Relationship Id="rId1" Type="{relationships}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>'
        ),
        'xl/worksheets/sheet1.xml': (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{main}">'
            f'<sheetData>{"".join(sheet_rows)}</sheetData></worksheet>'
        ),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()
