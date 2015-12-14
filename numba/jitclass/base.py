from __future__ import absolute_import, print_function
import types as pytypes
import inspect
from numba.utils import OrderedDict
from collections import Sequence
from numba import types
from numba.targets.registry import CPUTarget
from numba import njit
from numba.typing import templates
from numba.datamodel import default_manager, models
from numba.targets import imputils
from numba import cgutils
from llvmlite import ir as llvmir
from numba.six import exec_
from . import boxing


##############################################################################
# Data model

class InstanceModel(models.StructModel):
    def __init__(self, dmm, fe_typ):
        cls_data_ty = types.ClassDataType(fe_typ)
        # MemInfoPointer uses the `dtype` attribute to traverse for nested
        # NRT MemInfo.  Since we handle nested NRT MemInfo ourselves,
        # we will replace provide MemInfoPointer with an opaque type
        # so that it does not raise exception for nested meminfo.
        dtype = types.Opaque('Opaque.' + str(cls_data_ty))
        members = [
            ('meminfo', types.MemInfoPointer(dtype)),
            ('data', types.CPointer(cls_data_ty)),
        ]
        super(InstanceModel, self).__init__(dmm, fe_typ, members)


class InstanceDataModel(models.StructModel):
    def __init__(self, dmm, fe_typ):
        clsty = fe_typ.class_type
        members = list(clsty.struct.items())
        super(InstanceDataModel, self).__init__(dmm, fe_typ, members)


default_manager.register(types.ClassInstanceType, InstanceModel)
default_manager.register(types.ClassDataType, InstanceDataModel)
default_manager.register(types.ClassType, models.OpaqueModel)

##############################################################################
# Class object

_ctor_template = """
def ctor({args}):
    return __numba_cls_({args})
"""


class JitClassType(type):
    """
    The type of any jitclass.
    """
    def __new__(cls, name, bases, dct):
        if len(bases) != 1:
            raise TypeError("must have exactly one base class")
        [base] = bases
        if isinstance(base, JitClassType):
            raise TypeError("cannot subclass from a jitclass")
        assert 'class_type' in dct, 'missing "class_type" attr'
        outcls = type.__new__(cls, name, bases, dct)
        outcls._set_init()
        return outcls

    def _set_init(cls):
        # make ctor
        init = cls.class_type.instance_type.methods['__init__']
        argspec = inspect.getargspec(init)
        assert not argspec.varargs, 'varargs not supported'
        assert not argspec.keywords, 'keywords not supported'
        assert not argspec.defaults, 'defaults not supported'
        args = ', '.join(argspec.args[1:])
        ctor_source = _ctor_template.format(args=args)
        glbls = {"__numba_cls_": cls}
        exec_(ctor_source, glbls)
        ctor = glbls['ctor']
        cls._ctor = njit(ctor)

    def __instancecheck__(cls, instance):
        if isinstance(instance, boxing.Box):
            return instance._numba_type_.class_type is cls.class_type
        return False

    def __call__(cls, *args, **kwargs):
        return cls._ctor(*args, **kwargs)


##############################################################################
# Registration utils

def register_class_type(cls, spec, class_ctor, builder):
    """
    Internal function to create a jitclass.

    Args
    ----
    cls: the original class object (used as the prototype)
    spec: the structural specification contains the field types.
    class_ctor: the numba type to represent the jitclass
    builder: the internal jitclass builder
    """
    # Normalize spec
    if isinstance(spec, Sequence):
        spec = OrderedDict(spec)

    # Copy methods from base classes
    clsdct = {}
    for basecls in reversed(inspect.getmro(cls)):
        clsdct.update(basecls.__dict__)

    methods = dict((k, v) for k, v in clsdct.items()
                   if isinstance(v, pytypes.FunctionType))
    others = dict((k, v) for k, v in clsdct.items() if k not in methods)
    docstring = others.pop('__doc__', "")
    _drop_ignored_attrs(others)
    if others:
        msg = "class members are not yet supported: {0}"
        members = ', '.join(others.keys())
        raise TypeError(msg.format(members))

    class_type = class_ctor(cls, spec, methods)
    cls = JitClassType(cls.__name__, (cls,), dict(class_type=class_type,
                                                  __doc__=docstring))

    jitmethods = {}
    for k, v in methods.items():
        jitmethods[k] = njit(v)

    # Register resolution of the class object
    typer = CPUTarget.typing_context
    typer.insert_global(cls, class_type)

    # Register class
    backend = CPUTarget.target_context
    builder(class_type, jitmethods, methods, typer, backend).register()

    return cls


def _drop_ignored_attrs(dct):
    # ignore anything defined by object
    drop = set(['__weakref__',
                '__module__',
                '__dict__'])
    for k, v in dct.items():
        if isinstance(v, (pytypes.BuiltinFunctionType,
                          pytypes.BuiltinMethodType)):
            drop.add(k)
        elif getattr(v, '__objclass__', None) is object:
            drop.add(k)

    for k in drop:
        del dct[k]

class ClassBuilder(object):
    """
    A jitclass builder for mutable jitclasses.
    """
    instance_type_class = types.ClassInstanceType

    def __init__(self, class_type, jitmethods, methods, typer, backend):
        self.class_type = class_type
        self.jitmethods = jitmethods
        self.methods = methods
        self.typer = typer
        self.backend = backend

    def register(self):
        """
        Register to the frontend and backend.
        """
        outer = self

        class ConstructorTemplate(templates.AbstractTemplate):
            key = outer.class_type

            def generic(self, args, kws):
                ctor = outer.jitmethods['__init__']

                instance_type = outer.class_type.instance_type
                boundargs = (instance_type.get_reference_type(),) + args
                template, args, kws = ctor.get_call_template(boundargs, kws)
                sig = template(self.context).apply(args, kws)
                out = templates.signature(instance_type, *sig.args[1:])
                return out

        self.typer.insert_function(ConstructorTemplate(self.typer))

        instance_type = outer.class_type.instance_type
        outer.implement_frontend(instance_type)
        outer.implement_backend(instance_type)

    def implement_frontend(self, instance_type):
        self.implement_attribute_typing(instance_type)

    def implement_attribute_typing(self, instance_type):
        outer = self

        class ClassAttribute(templates.AttributeTemplate):
            key = instance_type

            def __init__(self, context, instance):
                self.instance = instance
                super(ClassAttribute, self).__init__(context)

            def generic_resolve(self, instance, attr):
                if attr in instance.struct:
                    return instance.struct[attr]

                elif attr in instance.methods:
                    meth = outer.jitmethods[attr]

                    class MethodTemplate(templates.AbstractTemplate):
                        key = (instance_type, attr)

                        def generic(self, args, kws):
                            args = (instance,) + tuple(args)
                            template, args, kws = meth.get_call_template(args,
                                                                         kws)
                            sig = template(self.context).apply(args, kws)
                            sig = templates.signature(sig.return_type,
                                                      *sig.args[1:],
                                                      recvr=sig.args[0])
                            return sig

                    return types.BoundFunction(MethodTemplate, instance)

        attrspec = ClassAttribute(self.typer, instance_type)
        self.typer.insert_attributes(attrspec)

    def implement_backend(self, instance_type):
        registry = imputils.Registry()
        ctor_nargs = len(self.jitmethods['__init__']._pysig.parameters) - 1
        self.implement_constructor(registry, instance_type, ctor_nargs)
        self.register_attributes_methods(registry, instance_type)

    def implement_constructor(self, registry, instance_type, ctor_nargs):
        def imp_dtor(context, module):
            llvoidptr = context.get_value_type(types.voidptr)
            llsize = context.get_value_type(types.uintp)
            dtor_ftype = llvmir.FunctionType(llvmir.VoidType(),
                                             [llvoidptr, llsize, llvoidptr])

            fname = "_Dtor.{0}".format(instance_type.name)
            dtor_fn = module.get_or_insert_function(dtor_ftype,
                                                    name=fname)
            if dtor_fn.is_declaration:
                # Define
                builder = llvmir.IRBuilder(dtor_fn.append_basic_block())

                alloc_fe_type = instance_type.get_data_type()
                alloc_type = context.get_value_type(alloc_fe_type)

                data_struct = cgutils.create_struct_proxy(alloc_fe_type)

                ptr = builder.bitcast(dtor_fn.args[0], alloc_type.as_pointer())
                data = data_struct(context, builder, ref=ptr)

                context.nrt_decref(builder, alloc_fe_type, data._getvalue())

                builder.ret_void()

            return dtor_fn

        @registry.register
        @imputils.implement(self.class_type, *([types.Any] * ctor_nargs))
        def ctor_impl(context, builder, sig, args):
            # Allocate the instance
            inst_typ = sig.return_type
            alloc_type = context.get_data_type(inst_typ.get_data_type())
            alloc_size = context.get_abi_sizeof(alloc_type)

            meminfo = context.nrt_meminfo_alloc_dtor(
                builder,
                context.get_constant(types.uintp, alloc_size),
                imp_dtor(context, builder.module),
            )
            data_pointer = context.nrt_meminfo_data(builder, meminfo)
            data_pointer = builder.bitcast(data_pointer,
                                           alloc_type.as_pointer())

            # Nullify all data
            builder.store(cgutils.get_null_value(alloc_type),
                          data_pointer)

            inst_struct_typ = cgutils.create_struct_proxy(inst_typ)
            inst_struct = inst_struct_typ(context, builder)
            inst_struct.meminfo = meminfo
            inst_struct.data = data_pointer

            # Call the __init__
            # TODO: extract the following into a common util
            init_sig = (sig.return_type,) + sig.args

            init = self.jitmethods['__init__']
            init.compile(init_sig)
            cres = init._compileinfos[init_sig]
            realargs = [inst_struct._getvalue()] + list(args)
            context.call_internal(builder, cres.fndesc, types.void(*init_sig),
                                  realargs)

            # Prepare reutrn value
            ret = inst_struct._getvalue()

            # Add function to link
            codegen = context.codegen()
            codegen.add_linking_library(cres.library)

            return imputils.impl_ret_new_ref(context, builder, inst_typ, ret)

    def register_attributes_methods(self, registry, instance_type):
        # Add attributes
        for attr in instance_type.struct:
            self.implement_attribute(registry, instance_type, attr)

        # Add methods
        for meth in instance_type.methods:
            self.implement_method(registry, instance_type, meth)

        self.backend.insert_func_defn(registry.functions)
        self.backend.insert_attr_defn(registry.attributes)

    def implement_attribute(self, registry, instance_type, attr):
        @registry.register_attr
        @imputils.impl_attribute(instance_type, attr)
        def imp(context, builder, typ, value):
            inst_struct = cgutils.create_struct_proxy(typ)
            inst = inst_struct(context, builder, value=value)
            data_pointer = inst.data
            data_struct = cgutils.create_struct_proxy(typ.get_data_type(),
                                                      kind='data')
            data = data_struct(context, builder, ref=data_pointer)
            return imputils.impl_ret_borrowed(context, builder,
                                              typ.struct[attr],
                                              getattr(data, attr))

    def implement_method(self, registry, instance_type, attr):
        nargs = len(self.jitmethods[attr]._pysig.parameters)

        @registry.register
        @imputils.implement((instance_type, attr), *([types.Any] * nargs))
        def imp(context, builder, sig, args):
            method = self.jitmethods[attr]
            method.compile(sig)
            cres = method._compileinfos[sig.args]
            out = context.call_internal(builder, cres.fndesc, sig, args)
            return imputils.impl_ret_new_ref(context, builder,
                                             sig.return_type, out)
