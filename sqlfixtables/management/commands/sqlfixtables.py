from django import get_version
from django.core.management.base import AppCommand
from django.conf import settings
from django.db import connection, models
from django.db.utils import DatabaseError
from optparse import make_option
import re
import sys

# Since this thing relies on django internals that are not guaranteed to be stable,
# do a strict version check.
COMPAT_MIN = '1.0'
COMPAT_MAX = '1.4.9999'

class Command(AppCommand):
    help = """Print SQL statements for model changes, including
- ALTER TABLE to add/drop columns
- ALTER TABLE to change the length of VARCHAR fields
- ALTER TABLE to change default values
- CREATE INDEX for new ForeignKey and OneToOne fields
- CREATE TABLE for new m2m relations

Not (yet) supported:
- Databases other than MySQL (sqlite will never be supported, others can be)
- Changes in unique_together
- Field type changes (they will be detected and warned about though)
- Index additions/removals
- Deleting old m2m tables
- Parent changes in multi-table inheritance

If you need those, a complete migration framework like django-south is a
better option for you.
"""
    output_transaction = True
    option_list = AppCommand.option_list + (
        make_option('--drop-columns', dest="drop_columns", action="store_true", default=False,
                    help="Drop columns that no longer exist in the model"),
    )

    def handle_app(self, app, **options):
        version = get_version()
        if version < COMPAT_MIN or version > COMPAT_MAX:
            print "This command is not compatible with django version %s" % version
            sys.exit(1)
        return (u'\n%s\n' % u'\n'.join(sql_fix_table(app, options['drop_columns'],  self.style))).encode('utf-8')

def sql_fix_table(app, drop_columns, style):
    if settings.DATABASE_ENGINE == 'dummy':
        # This must be the "dummy" database backend, which means the user
        # hasn't set DATABASE_ENGINE.
        raise CommandError("Django doesn't know which syntax to use for your SQL statements,\n" +
            "because you haven't specified the DATABASE_ENGINE setting.\n" +
            "Edit your settings file and change DATABASE_ENGINE to something like 'postgresql' or 'mysql'.")
    if settings.DATABASE_ENGINE != 'mysql':
        raise CommandError("This has only been tested with MySQL and probably doesn't work for others")

    all_models = models.get_models()
    if get_version() < '1.2':
        app_models = models.get_models(app)
    else:
        app_models = models.get_models(app, include_auto_created=True)
    pending_references = {}
    final_output = []

    # Fix up tables
    for model in app_models:
        # Handle opts.unique_together (removals) TODO.
        sql, references = sql_alter_table(connection, model, style, all_models, drop_columns)
        # Handle opts.unique_together (additions) TODO.
        # Handle pending references
        for refto, refs in references.items():
            pending_references.setdefault(refto, []).extend(refs)
            if refto in all_models:
                sql.extend(connection.creation.sql_for_pending_references(refto, style, pending_references))
        sql.extend(sql_new_many_to_many(connection, model, style, connection.introspection.table_names()))

        final_output.extend(sql) 
    return final_output

def sql_alter_table(connection, model, style, known_models, drop_columns):
    # Do nothing fo unmanaged models
    opts = model._meta
    if not opts.managed or opts.proxy:
        return [], {}

    qn = connection.ops.quote_name
    fields = dict([(x.column, x) for x in opts.local_fields])
    cursor = connection.cursor()
    try:
        cursor.execute('DESCRIBE %s' % qn(opts.db_table))
    except DatabaseError:
        # This is a new m2m table, do a full creation
        sql, references = connection.creation.sql_create_model(model, style, known_models)
        return sql, references
    final_output = []
    m2m_sql = []
    pending_references = {}

    for f_name, f_type, f_null, f_key, f_default, f_extra in cursor.fetchall():
        # Skip _ptr_id fields. Parent handling not yet implemented. TODO
        if f_name.endswith('_ptr_id'):
            continue

        field = fields.pop(f_name, None)

        # Drop columns no longer relevant
        if not field:
            final_output.append(style.NOTICE('-- Field %s no longer exists in the %s model' % (f_name, model.__name__)))
            if drop_columns:
                final_output.append(' '.join([style.SQL_KEYWORD('ALTER TABLE'),
                    style.SQL_TABLE(qn(opts.db_table)),
                    style.SQL_KEYWORD('DROP COLUMN'),
                    style.SQL_FIELD(qn(f_name)),
                    ]) + ';')
            continue

        # Fix up a few things
        modify_column = False

        # varchar size
        n_type = field.db_type().lower()
        f_type_l = f_type.lower()
        if not are_equivalent(f_type_l, n_type):
            modify_column = True
            if f_type_l[:f_type_l.find('(')] != n_type[:n_type.find('(')]:
                final_output.append(
                        style.NOTICE('-- Field %s.%s type changed from %s to %s, this cannot be fixed automatically' %
                        (model.__name__, field.name, f_type, field.db_type())))
                continue
            final_output.append(style.NOTICE('-- Field %s.%s type changed from %s to %s' %
                                (model.__name__, field.name, f_type, field.db_type())))

        # null/not null
        if (f_null.lower() == 'yes') != field.null:
            final_output.append(style.NOTICE('-- Field %s.%s changed nullness requirements' % (model.__name__, field.name)))
            modify_column = True
        if (f_key.lower() in ('uni','pri')) != field.unique:
            final_output.append(style.NOTICE('-- Field %s.%s changed uniqueness requirements' % (model.__name__, field.name)))
            modify_column = True

        if modify_column:
            field_output = [style.SQL_KEYWORD('ALTER TABLE'),
                style.SQL_TABLE(qn(opts.db_table)),
                style.SQL_KEYWORD('MODIFY COLUMN'),
                style.SQL_FIELD(qn(f_name)),
                style.SQL_COLTYPE(field.db_type()),
                ]
            if not field.null:
                field_output.append(style.SQL_KEYWORD('NOT NULL'))
            elif field.unique:
                field_output.append(style.SQL_KEYWORD('UNIQUE'))
            final_output.append(' '.join(field_output)+';')

    # Create new columns
    for f in fields.values():
        if f.name.endswith('_ptr'):
            # TODO: parent handling
            continue
        col_type = f.db_type()
        tablespace = f.db_tablespace or opts.db_tablespace

        # Make the definition (e.g. 'foo VARCHAR(30)') for this field.
        field_output = [style.SQL_KEYWORD('ALTER TABLE'),
            style.SQL_TABLE(qn(opts.db_table)),
            style.SQL_KEYWORD('ADD COLUMN'),
            style.SQL_FIELD(qn(f.column)),
            style.SQL_COLTYPE(col_type)]
        if not f.null:
            field_output.append(style.SQL_KEYWORD('NOT NULL'))
        if f.primary_key:
            field_output.append(style.SQL_KEYWORD('PRIMARY KEY'))
        elif f.unique:
            field_output.append(style.SQL_KEYWORD('UNIQUE'))
        if tablespace and f.unique:
            # We must specify the index tablespace inline, because we
            # won't be generating a CREATE INDEX statement for this field.
            field_output.append(connection.ops.tablespace_sql(tablespace, inline=True))
        if f.rel:
            ref_output, pending = connection.creation.sql_for_inline_foreign_key_references(f, known_models, style)
            if pending:
                pr = pending_references.setdefault(f.rel.to, []).append((model, f))
            else:
                field_output.extend(ref_output)
        final_output.append(' '.join(field_output)+';')
        final_output.extend(connection.creation.sql_indexes_for_field(model, f, style))
    
    return final_output, pending_references

def sql_new_many_to_many(connection, model, style, all_tables):
    "Return the CREATE TABLE statments for all the many-to-many tables defined on a model"

    output = []
    for f in model._meta.local_many_to_many:
        if f.m2m_db_table() in all_tables:
            continue
        if model._meta.managed or f.rel.to._meta.managed:
            output.extend(connection.creation.sql_for_many_to_many_field(model, f, style))
    return output

equivalence_mapping = {
    'integer': re.compile(r'int\(\d+\)'),
    'integer auto_increment': re.compile(r'int\(\d+\)'),
    'integer unsigned': re.compile(r'int\(\d+\)'),
    'bool': 'tinyint(1)',
}
def are_equivalent(old, new):
    if old == new:
        return True
    if new not in equivalence_mapping:
        return False

    old_ = equivalence_mapping[new]
    if old_ == old:
        return True
    if hasattr(old_, 'match') and old_.match(old):
        return True
    return False
