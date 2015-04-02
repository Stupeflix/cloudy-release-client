import tempfile
import sys
import os
import os.path as op
import subprocess
import logging
import contextlib
import threading
import json
import traceback
import collections

import yaml
import jinja2

from cloudyclient.state import load_data
from cloudyclient.exceptions import TemplateError
from cloudyclient.api.shell import ShellVariables


logger = logging.getLogger(__name__)
_globals = threading.local()
no_default = object()


def get_global(name, default=no_default):
    '''
    Get a global variable by *name*.

    If *default* is specified, initialize the global with this value if if does
    not exist already.
    '''
    if default is not no_default:
        return _globals.__dict__.setdefault(name, default)
    return _globals.__dict__[name]


def set_global(name, value):
    '''
    Set global variable *name* to *value*.
    '''
    _globals.__dict__[name] = value


def run(*cmd_args, **kwargs):
    '''
    Run a subprocess with the list of arguments *cmd_args*.

    Logging can be controlled with the *log_command* and *log_pipes* keyword
    arguments. They both default to true, and control output of the command
    arguments and its stderr/stdout pipes.

    If *no_pipes* is true (default is false), stderr and stdout are not
    intercepted.

    Additional keyword arguments are passed to the :class:`subprocess.Popen`
    constructor.

    Returns the subprocess' stdout on success, or raises
    :class:`subprocess.CalledProcessError` if the command returns a non-zero
    exit status.
    '''
    log_pipes = kwargs.pop('log_pipes', True)
    no_pipes = kwargs.pop('no_pipes', False)
    cmd_string = ' '.join(cmd_args)
    logger.info(cmd_string)
    if get_global('dry_run', False):
        return ''
    cwd_stack = get_global('cwd_stack', [])
    if cwd_stack and 'cwd' not in kwargs:
        kwargs['cwd'] = cwd_stack[-1]
    if not no_pipes:
        kwargs['stdout'] = subprocess.PIPE
        kwargs['stderr'] = subprocess.PIPE
    process = subprocess.Popen(cmd_args, **kwargs)
    stdout, stderr = process.communicate()
    if not no_pipes:
        stdout = stdout.strip()
        stderr = stderr.strip()
    if log_pipes and stdout:
        logger.info('stdout:\n%s', stdout.decode('utf8'))
    if log_pipes and stderr:
        logger.info('stderr:\n%s', stderr.decode('utf8'))
    retcode = process.poll()
    if retcode:
        error = subprocess.CalledProcessError(retcode, cmd_string)
        logger.error(str(error))
        raise error
    return stdout


def sudo(*cmd_args, **kwargs):
    '''
    Shortcut to run a command with sudo.

    Accepts same parameters as :func:`run`.
    '''
    return run('sudo', *cmd_args, **kwargs)


@contextlib.contextmanager
def cd(path):
    '''
    A context manager that can be used to change the current working directory
    of processes started by :func:`run`.
    '''
    logger.info('cd %s', path)
    cwd_stack = get_global('cwd_stack', [])
    cwd_stack.append(path)
    try:
        yield
    finally:
        cwd_stack.pop()
        if cwd_stack:
            cwd = cwd_stack[-1]
        else:
            cwd = os.getcwd()
        logger.info('cd %s', cwd)


def getcwd():
    '''
    Get the current working directory, as set by :func:`cd`.
    '''
    cwd_stack = get_global('cwd_stack', [])
    if cwd_stack:
        cwd = cwd_stack[-1]
    else:
        cwd = os.getcwd()
    return cwd


@contextlib.contextmanager
def dry_run():
    '''
    A context manager that inhibits :func:`run` from running any subprocess
    during its lifetime.
    '''
    set_global('dry_run', True)
    try:
        yield
    finally:
        set_global('dry_un', False)


def find_deployment_data(base_dir=None):
    '''
    Retrieve deployment data by searching from *base_dir* and up.

    If *base_dir* is not specified, :func:`getcwd` is used.

    Returns the data dict, or None if it was not found.
    '''
    if base_dir is None:
        base_dir = getcwd()
    base_dir = op.abspath(base_dir)
    while base_dir != '/':
        base_dir, tail = op.split(base_dir)
        project_name, _, index = tail.rpartition('.')
        project_name = project_name.lstrip('.')
        if index.isdigit():
            # Looks like we found a checkout dir, see if we can load data
            data = load_data(base_dir, project_name)
            if data:
                return data


def find_deployment_variables(base_dir=None):
    '''
    Retrieve deployment variables by searching from *base_dir* and up.

    If *base_dir* is not specified, :func:`os.getcwd` is used.

    Returns the variables dict, or None if it was not found.
    '''
    data = find_deployment_data(base_dir=base_dir)
    if data is not None:
        return merge_deployment_variables(data)


def merge_deployment_variables(data):
    '''
    Merge the deployment variables in the *data* :class:`dict`.
    '''
    if data.get('base_variables_format') is not None:
        base_variables = decode_deployment_variables(
                data['base_variables_format'],
                data['base_variables'])
    else:
        base_variables = {}
    variables = decode_deployment_variables(data['variables_format'],
            data['variables'])
    if isinstance(variables, ShellVariables) and not base_variables:
        base_variables = ShellVariables('')
    base_variables.update(variables)
    return base_variables


def decode_deployment_variables(variables_format, variables):
    '''
    Decode raw variables *variables* from *variables_format*.
    '''
    if variables_format == 'json':
        ret = json.loads(variables)
    elif variables_format == 'yaml':
        ret = yaml.safe_load(variables)
    elif variables_format == 'python':
        code = compile(variables, '<deployment variables>', 'exec')
        code_globals = {}
        exec(code, code_globals)
        code_globals.pop('__builtins__', None)
        ret = code_globals
    elif variables_format == 'shell':
        ret = ShellVariables(variables)
    else:
        raise ValueError('unknwon variables format "%s"' %
                variables_format)
    if not isinstance(ret, collections.Mapping):
        raise TypeError('deployment variables must be a mapping')
    return ret


def render_template(source, destination, context={}, use_jinja=False,
        use_sudo=False):
    '''
    Render template file *source* to *destination*.

    The contents of the source file are formatted with *context*, passed to the
    :meth:`basestring.format` method, or to jinja if *use_jinja* is True.

    If *use_sudo* is true, first save the rendered template to a temporary
    file, then copy it to its final position with sudo.
    '''
    # Read template source
    with open(source) as fp:
        template_src = fp.read()

    # Render template
    if use_jinja:
        template = jinja2.Template(template_src, undefined=jinja2.StrictUndefined)
        try:
            rendered = template.render(**context)
        except jinja2.TemplateSyntaxError as exc:
            line = traceback.extract_tb(sys.exc_info()[2])[-1][1]
            context = _get_template_context(template_src, line)
            error = '{0}; line {1} in template:\n\n{2}'.format(
                    exc, line, context)
            raise TemplateError(error)
        except jinja2.UndefinedError as exc:
            line = traceback.extract_tb(sys.exc_info()[2])[-1][1]
            context = _get_template_context(template_src, line)
            error = '{0}; line {1} in template\n{2}'.format(
                    exc, line, context)
            raise TemplateError(error)
    else:
        rendered = template_src.format(**context)

    # Save rendered template to destination
    if not use_sudo:
        with open(destination, 'w') as fp:
            fp.write(rendered)
    else:
        fp = tempfile.NamedTemporaryFile(prefix='cloudyclient-', delete=False)
        fp.write(rendered)
        fp.close()
        sudo('mv', fp.name, destination)
        sudo('chmod', '644', destination)


def _get_template_context(template, line, num_lines=5):
    '''
    Returns debugging context around a line in a given template.

    Returns:: string
    '''
    marker = '    <======================'
    template_lines = template.splitlines()
    num_template_lines = len(template_lines)

    # in test, a single line template would return a crazy line number like,
    # 357.  do this sanity check and if the given line is obviously wrong, just
    # return the entire template
    if line > num_template_lines:
        return template

    context_start = max(0, line - num_lines - 1)  # subtract 1 for 0-based indexing
    context_end = min(num_template_lines, line + num_lines)
    error_line_in_context = line - context_start - 1  # subtract 1 for 0-based indexing

    buf = []
    if context_start > 0:
        buf.append('[...]')
        error_line_in_context += 1

    buf.extend(template_lines[context_start:context_end])

    if context_end < num_template_lines:
        buf.append('[...]')

    if marker:
        buf[error_line_in_context] += marker

    return '---\n{0}\n---'.format('\n'.join(buf))


def copy_system_package(pkg, sys_python='/usr/bin/python',
        venv_python='python'):
    '''
    Copy system-wide *pkg* in a virtualenv.

    *sys_python* can be given to customize the system python executable
    path. The default is '/usr/bin/python'.

    *venv_python* is used to retrieve the virtualenv's site-packages directory,
    and defaults to 'python'.
    '''
    # Get package path in the system packages
    pkg_file = run(sys_python, '-c',
            'import {0}; print {0}.__file__'.format(pkg))
    # Get site-packages path in the virtualenv
    venv_site_packages = run(venv_python, '-c',
        'from distutils.sysconfig import get_python_lib; print(get_python_lib())')
    # Copy package in virtualenv
    if '__init__.py' in pkg_file:
        # Dealing with a package
        src_dir = op.dirname(pkg_file)
        basename = op.basename(src_dir)
        dst_dir = op.join(venv_site_packages, basename)
        if op.exists(dst_dir):
            run('rm', '-rf', dst_dir)
        run('cp', '-RL', src_dir, dst_dir)
    else:
        # Dealing with a top-level module
        dest_file = op.join(venv_site_packages, op.split(pkg_file)[1])
	if op.exists(dest_file):
            run('mv', dest_file, dest_file + '.backup')
        try:
            run('cp', '-RL', pkg_file, venv_site_packages)
        except Exception:
            # revert
	    if op.exists(dest_file + '.backup'):
                run('mv', dest_file + '.backup', dest_file)

