#!/usr/bin/python
# -*- coding: utf-8 -*-

# (c) 2012-2013, Timothy Appnel <tim@appnel.com>
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import sys
import os.path

from ansible.plugins.action import ActionBase
from ansible.plugins import connection_loader
from ansible.utils.boolean import boolean
from ansible import constants


class ActionModule(ActionBase):

    def _get_absolute_path(self, path):
        if self._task._role is not None:
            original_path = path
            path = self._loader.path_dwim_relative(self._task._role._role_path, 'files', path)
            if original_path and original_path[-1] == '/' and path[-1] != '/':
                # make sure the dwim'd path ends in a trailing "/"
                # if the original path did
                path += '/'

        return path

    def _process_origin(self, host, path, user):

        if not host in ['127.0.0.1', 'localhost']:
            if user:
                return '%s@%s:%s' % (user, host, path)
            else:
                return '%s:%s' % (host, path)
        else:
            if not ':' in path:
                if not path.startswith('/'):
                    path = self._get_absolute_path(path=path)
            return path

    def _process_remote(self, host, path, user):
        transport = self._play_context.connection
        return_data = None
        if not host in ['127.0.0.1', 'localhost'] or transport != "local":
            if user:
                return_data = '%s@%s:%s' % (user, host, path)
            else:
                return_data = '%s:%s' % (host, path)
        else:
            return_data = path

        if not ':' in return_data:
            if not return_data.startswith('/'):
                return_data = self._get_absolute_path(path=return_data)

        return return_data

    def run(self, tmp=None, task_vars=dict()):
        ''' generates params and passes them on to the rsync module '''

        original_transport = task_vars.get('ansible_connection') or self._play_context.connection
        transport_overridden = False
        if task_vars.get('delegate_to') is None:
            task_vars['delegate_to'] = '127.0.0.1'
            # IF original transport is not local, override transport and disable sudo.
            if original_transport != 'local':
                task_vars['ansible_connection'] = 'local'
                transport_overridden = True
                self._play_context.become = False

        use_ssh_args = self._task.args.pop('use_ssh_args', None)

        # Parameter name needed by the ansible module
        self._task.args['_local_rsync_path'] = task_vars.get('ansible_rsync_path') or 'rsync'

        # from the perspective of the rsync call the delegate is the localhost
        src_host = '127.0.0.1'
        dest_host = task_vars.get('ansible_ssh_host') or task_vars.get('inventory_hostname')

        ### FIXME: do we still need to explicitly template ansible_ssh_host here in v2?

        dest_is_local = dest_host in ['127.0.0.1', 'localhost']


        # CHECK FOR NON-DEFAULT SSH PORT
        dest_port = task_vars.get('ansible_ssh_port') or self._task.args.get('dest_port') or 22

        # CHECK DELEGATE HOST INFO
        use_delegate = False

        if dest_host == task_vars.get('delegate_to'):
            # edge case: explicit delegate and dest_host are the same
            dest_host = '127.0.0.1'
            use_delegate = True
        else:
            if 'hostvars' in task_vars:
                if task_vars.get('delegate_to') in task_vars['hostvars'] and original_transport != 'local':
                    # use a delegate host instead of localhost
                    use_delegate = True

        # COMPARE DELEGATE, HOST AND TRANSPORT
        process_args = False
        if not dest_host is src_host and original_transport != 'local':
            # interpret and task_vars remote host info into src or dest
            process_args = True

        # SWITCH SRC AND DEST PER MODE
        if self._task.args.get('mode', 'push') == 'pull':
            (dest_host, src_host) = (src_host, dest_host)

        # Delegate to localhost as the source of the rsync unless we've been
        # told (via delegate_to) that a different host is the source of the
        # rsync
        if not use_delegate:
            # Create a connection to localhost to run rsync on
            ### FIXME: Do we have to dupe stdin or is this sufficient?
            new_stdin = self._connection._new_stdin
            new_connection = connection_loader.get('local', self._play_context, new_stdin)
            self._connection = new_connection

        # MUNGE SRC AND DEST PER REMOTE_HOST INFO
        src  = self._task.args.get('src', None)
        dest = self._task.args.get('dest', None)
        if process_args or use_delegate:

            user = None
            if boolean(task_vars.get('set_remote_user', 'yes')):
                if use_delegate:
                    user = task_vars['hostvars'][conn.delegate].get('ansible_ssh_user')

                if not use_delegate or not user:
                    user = task_vars.get('ansible_ssh_user') or self._play_context.remote_user

            if use_delegate:
                # FIXME
                private_key = task_vars.get('ansible_ssh_private_key_file') or self._play_context.private_key_file
            else:
                private_key = task_vars.get('ansible_ssh_private_key_file') or self._play_context.private_key_file

            if private_key is not None:
                private_key = os.path.expanduser(private_key)
                self._task.args['private_key'] = private_key

            # use the mode to define src and dest's url
            if self._task.args.get('mode', 'push') == 'pull':
                # src is a remote path: <user>@<host>, dest is a local path
                src  = self._process_remote(src_host, src, user)
                dest = self._process_origin(dest_host, dest, user)
            else:
                # src is a local path, dest is a remote path: <user>@<host>
                src  = self._process_origin(src_host, src, user)
                dest = self._process_remote(dest_host, dest, user)

        self._task.args['src'] = src
        self._task.args['dest'] = dest

        # Remove mode as it is handled purely in this action module
        if 'mode' in self._task.args:
            del self._task.args['mode']

        # Allow custom rsync path argument.
        rsync_path = self._task.args.get('rsync_path', None)

        # If no rsync_path is set, sudo was originally set, and dest is remote then add 'sudo rsync' argument.
        if not rsync_path and transport_overridden and self._play_context.become and self._play_context.become_method == 'sudo' and not dest_is_local:
            rsync_path = 'sudo rsync'

        # make sure rsync path is quoted.
        if rsync_path:
            self._task.args['rsync_path'] = '"%s"' % rsync_path

        if use_ssh_args:
            self._task.args['ssh_args'] = constants.ANSIBLE_SSH_ARGS

        # run the module and store the result
        result = self._execute_module('synchronize', task_vars=task_vars)

        return result
