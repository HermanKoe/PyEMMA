"""
This module contains custom serialization handlers for jsonpickle to flatten and restore given types.

@author: Martin K. Scherer
"""

import numpy as np
from io import BytesIO

from jsonpickle import handlers
from jsonpickle import util


def register_ndarray_handler():
    """ Override jsonpickle handler for numpy arrays with compressed NPZ handler.
    First unregisters the default handler
    """
    from jsonpickle.ext.numpy import unregister_handlers
    unregister_handlers()
    NumpyNPZHandler.handles(np.ndarray)


def unregister_ndarray_handler():
    """ Restore jsonpickle default numpy array handler.
    """
    from jsonpickle.handlers import unregister
    from jsonpickle.ext.numpy import register_handlers
    unregister(np.ndarray)
    register_handlers()


class NumpyNPZHandler(handlers.BaseHandler):
    """ stores NumPy array as a compressed NPZ file. """
    def __init__(self, context):
        super(NumpyNPZHandler, self).__init__(context=context)

    def flatten(self, obj, data):
        assert isinstance(obj, np.ndarray)
        buff = BytesIO()
        np.savez_compressed(buff, x=obj)
        buff.seek(0)
        flattened_bytes = util.b64encode(buff.read())
        data['npz_file_bytes'] = flattened_bytes
        return data

    def restore(self, obj):
        binary = util.b64decode(obj['npz_file_bytes'])
        buff = BytesIO(binary)
        fh = np.load(buff)
        array = fh['x']
        fh.close()
        return array