"""Schema operations that skip what the database already has.

The lead follow-up migrations (0002, 0003) and the Work activity migration (0004) add some of the same columns, indexes
and constraints. A database migrated on the Work activities branch before the two were combined (its old
0002_work_activities) already has those, so they are skipped there and added everywhere else; a database that already
applied these migrations never runs them again. What Django records about the models is the same as with the plain
operations. (The migration loader ignores this module: its name starts with an underscore.)
"""

from django.db import migrations


def _existing(schema_editor, table, kind):
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        if kind == 'columns':
            return {column.name for column in connection.introspection.get_table_description(cursor, table)}
        return set(connection.introspection.get_constraints(cursor, table))


class AddFieldIfMissing(migrations.AddField):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        if model._meta.get_field(self.name).column not in _existing(schema_editor, model._meta.db_table, 'columns'):
            super().database_forwards(app_label, schema_editor, from_state, to_state)


class AddIndexIfMissing(migrations.AddIndex):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        if self.index.name not in _existing(schema_editor, model._meta.db_table, 'constraints'):
            super().database_forwards(app_label, schema_editor, from_state, to_state)


class AddConstraintIfMissing(migrations.AddConstraint):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        if self.constraint.name not in _existing(schema_editor, model._meta.db_table, 'constraints'):
            super().database_forwards(app_label, schema_editor, from_state, to_state)
