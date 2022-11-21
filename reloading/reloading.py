import inspect
import sys
import ast
import traceback
import types
from itertools import chain
from functools import partial, update_wrapper


# have to make our own partial in case someone wants to use reloading as a iterator without any arguments
# they would get a partial back because a call without a iterator argument is assumed to be a decorator.
# getting a "TypeError: 'functools.partial' object is not iterable"
# which is not really descriptive.
# hence we overwrite the iter to make sure that the error makes sense.
class no_iter_partial(partial):
    def __iter__(self):
        raise TypeError(
            "Nothing to iterate over. Please pass an iterable to reloading."
        )


from collections import Iterable


def reloading(fn_or_seq=None, every=1, forever=None):
    """Wraps a loop iterator or decorates a function to reload the source code
    before every loop iteration or function invocation.

    When wrapped around the outermost iterator in a `for` loop, e.g.
    `for i in reloading(range(10))`, causes the loop body to reload from source
    before every iteration while keeping the state.
    When used as a function decorator, the decorated function is reloaded from
    source before each execution.

    Pass the integer keyword argument `every` to reload the source code
    only every n-th iteration/invocation.

    Args:
        fn_or_seq (function | iterable): A function or loop iterator which should
            be reloaded from source before each invocation or iteration,
            respectively
        every (int, Optional): After how many iterations/invocations to reload
        forever (bool, Optional): Pass `forever=true` instead of an iterator to
            create an endless loop

    """
    if fn_or_seq:
        fntypes = [types.FunctionType]
        # type(someclass) -> <class 'type'>
        try:
            if any(isinstance(fn_or_seq, fntype) for fntype in fntypes):
                return _reloading_function(fn_or_seq, every=every)
            elif isinstance(fn_or_seq, Iterable):
                return _reloading_loop(fn_or_seq, every=every)
            else:
                return _reloading_class(fn_or_seq, every=every)
        except:
            import traceback

            traceback.print_exc()
            print("UNKNOWN TYPE:", type(fn_or_seq))
            breakpoint()
    if forever:
        return _reloading_loop(iter(int, 1), every=every)

    # return this function with the keyword arguments partialed in,
    # so that the return value can be used as a decorator
    decorator = update_wrapper(no_iter_partial(reloading, every=every), reloading)
    return decorator


def unique_name(used):
    # get the longest element of the used names and append a "0"
    return max(used, key=len) + "0"


def format_itervars(ast_node):
    """Formats an `ast_node` of loop iteration variables as string, e.g. 'a, b'"""

    # handle the case that there only is a single loop var
    if isinstance(ast_node, ast.Name):
        return ast_node.id

    names = []
    for child in ast_node.elts:
        if isinstance(child, ast.Name):
            names.append(child.id)
        elif isinstance(child, ast.Tuple) or isinstance(child, ast.List):
            # if its another tuple, like "a, (b, c)", recurse
            names.append("({})".format(format_itervars(child)))

    return ", ".join(names)


def load_file(path):
    src = ""
    # while loop here since while saving, the file may sometimes be empty.
    while src == "":
        with open(path, "r") as f:
            src = f.read()
    return src + "\n"


def parse_file_until_successful(path):
    source = load_file(path)
    while True:
        try:
            tree = ast.parse(source)
            return tree
        except SyntaxError:
            handle_exception(path)
            source = load_file(path)


def isolate_loop_body_and_get_itervars(tree, lineno, loop_id):
    """Modifies tree inplace as unclear how to create ast.Module.
    Returns itervars"""
    candidate_nodes = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.For)
            and isinstance(node.iter, ast.Call)
            and node.iter.func.id == "reloading"
            and (
                (loop_id is not None and loop_id == get_loop_id(node))
                or getattr(node, "lineno", None) == lineno  # this is just a hack.
            )
        ):
            candidate_nodes.append(node)

    if len(candidate_nodes) > 1:
        raise LookupError(
            "The reloading loop is ambigious. Use `reloading` only once per line and make sure that the code in that line is unique within the source file."
        )

    if len(candidate_nodes) < 1:
        raise LookupError(
            "Could not locate reloading loop. Please make sure the code in the line that uses `reloading` doesn't change between reloads."
        )

    loop_node = candidate_nodes[0]
    tree.body = loop_node.body
    return loop_node.target, get_loop_id(loop_node)


def get_loop_id(ast_node):
    """Generates a unique identifier for an `ast_node` of type ast.For to find the loop in the changed source file"""
    return ast.dump(ast_node.target) + "__" + ast.dump(ast_node.iter)


def get_loop_code(loop_frame_info, loop_id, prefix="_RELOADING_"):
    fpath = loop_frame_info[1]
    mfpath = removePrefix(fpath, prefix=prefix)
    while True:
        tree = parse_file_until_successful(mfpath)
        try:
            itervars, found_loop_id = isolate_loop_body_and_get_itervars(
                tree,
                lineno=loop_frame_info[2],
                loop_id=loop_id,  # are you sure this will work?
            )
            return (
                compile(tree, filename=prefix + fpath, mode="exec"),
                format_itervars(itervars),
                found_loop_id,
            )
        except LookupError:
            handle_exception(fpath)


def handle_exception(fpath, prefix="_RELOADING_"):
    depth = fpath.count(prefix)
    mfpath = removePrefix(fpath)
    exc = traceback.format_exc()
    exc = exc.replace('File "<string>"', 'File "{}"'.format(mfpath))
    sys.stderr.write(f"Reloading Depth {depth}\n{exc}\n")
    hint = "Edit {} and press return to continue"
    # allow_exception = depth != 0
    allow_exception = True
    if allow_exception:
        # hint += ", 'e' for exception"
        hint += ", 'k' for skip, 'e' for exception"  # skip is to return None for the current function. maybe it will cause more problems.
    hint += "."
    print(hint.format(mfpath))
    # sys.stdin.readline()
    signal = input()
    signal_lower = signal.lower()
    if signal_lower == "k" and allow_exception:
        return True
    elif signal_lower == "e" and allow_exception:
        raise Exception('Raise exception for file: "{}"'.format(fpath))
    else:
        return False


def _reloading_loop(seq, every=1):
    loop_frame_info = inspect.stack()[2]
    fpath = loop_frame_info[1]

    caller_globals = loop_frame_info[0].f_globals
    caller_locals = loop_frame_info[0].f_locals

    # create a unique name in the caller namespace that we can safely write
    # the values of the iteration variables into
    unique = unique_name(chain(caller_locals.keys(), caller_globals.keys()))
    loop_id = None

    for i, itervar_values in enumerate(seq):
        if i % every == 0:
            compiled_body, itervars, loop_id = get_loop_code(
                loop_frame_info, loop_id=loop_id
            )

        caller_locals[unique] = itervar_values
        exec(itervars + " = " + unique, caller_globals, caller_locals)
        try:
            # run main loop body
            exec(compiled_body, caller_globals, caller_locals)
        except Exception:
            handle_exception(fpath)

    return []


def get_decorator_name(dec_node):
    if hasattr(dec_node, "id"):
        return dec_node.id
    return dec_node.func.id


def strip_reloading_decorator(func):
    """Remove the reloading decorator in-place"""
    func.decorator_list = [
        dec for dec in func.decorator_list if get_decorator_name(dec) != "reloading"
    ]


def isolate_function_def( # careful! we need to sort the definition.
    funcname, tree, codeInfos,funcdefs=[ast.FunctionDef, ast.AsyncFunctionDef]
):
    # if codeInfos is None, then we do not sort. we just return.
    """Strip everything but the function definition from the ast in-place.
    Also strips the reloading decorator from the function definition"""
    import copy
    nodeList = []
    for node in ast.walk(tree):
        if (
            any(isinstance(node, funcdef) for funcdef in funcdefs)
            and node.name == funcname
            and "reloading" in [get_decorator_name(dec) for dec in node.decorator_list]
        ):
        # please sort it in some way.
        # ['__class__', '__delattr__', '__dict__', '__dir__', '__doc__', '__eq__', '__format__', '__ge__', '__getattribute__', '__gt__', '__hash__', '__init__', '__init_subclass__', '__le__', '__lt__', '__module__', '__ne__', '__new__', '__reduce__', '__reduce_ex__', '__repr__', '__setattr__', '__sizeof__', '__str__', '__subclasshook__', '__weakref__', '_attributes', '_fields', 'args', 'body', 'col_offset', 'decorator_list', 'end_col_offset', 'end_lineno', 'lineno', 'name', 'returns', 'type_comment']
            # print("NODE?",node)
            # breakpoint()
            if codeInfos is None:
                strip_reloading_decorator(node)
                tree.body = [node]
                return True
            else:
                lineno = node.lineno
                # sort it out please!
                addrDistance = abs(codeInfos["lineNumber"] - lineno)
                nodeList.append(
                    (addrDistance,
                    copy.copy(node)) # append what? copy what? sort what?
                )
    nodeList.sort(key=lambda x: x[0])
    if codeInfos is not None and nodeList != []:
        # return nodeList[0][1]
        node = nodeList[0][1]
        strip_reloading_decorator(node)
        tree.body = [node]
        return True
    return False


def removePrefix(fpath, prefix="_RELOADING_"):
    mfpath = fpath
    while True:
        if mfpath.startswith(prefix):
            mfpath = mfpath[len(prefix) :]
        else:
            return mfpath


def get_function_def_code(
    fpath, fn, funcdefs=[ast.FunctionDef, ast.AsyncFunctionDef], prefix="_RELOADING_"
):
    mfpath = removePrefix(fpath, prefix=prefix)
    tree = parse_file_until_successful(mfpath)
    ## locate the freaking function definition!
    # print(dir(fn))
    funcString = str(fn.__code__)
    # <code object testfunc at 0x102876f50, file "reload_py_template.py", line 3>
    # what is the damn location of this code object?
    import parse
    # codeFormat = '<code object {} at 0x{}, file "{}", line {}>'
    # codeFormat = '<code object {funcName:s} at 0x{}, file "{fileName:s}", line {lineNumber:d}>'
    # codeFormat = '<code object {funcName:s} at 0x{codeAddress:x}, file "{fileName:s}", line {lineNumber:d}>'
    codeFormat = '<code object {funcName} at 0x{codeAddress}, file "{fileName}", line {lineNumber:d}>'
    codeInfos = parse.parse(codeFormat, funcString)
    # <Result () {'funcName': 'testfunc', 'codeAddress': '105152f50', 'fileName': 'reload_py_template.py', 'lineNumber': 3}>
    # print(codeInfos) # None?
    # print([funcString])
    # breakpoint()
    found = isolate_function_def(fn.__name__, tree, codeInfos,funcdefs=funcdefs)
    if not found:
        return None
    # print('tree fetched:',tree) # ast.Module object.
    compiled = compile(
        tree, filename=prefix + fpath, mode="exec"
    )  # filename is the same as the original name?
    # compiled = compile(tree, filename="", mode="exec") # filename is the same as the original name?
    return compiled


def get_reloaded_function(
    caller_globals,
    caller_locals,
    fpath,
    fn,
    funcdefs=[ast.FunctionDef, ast.AsyncFunctionDef],
):
    code = get_function_def_code(fpath, fn, funcdefs=funcdefs)
    if code is None:
        return None
    # need to copy locals, otherwise the exec will overwrite the decorated with the undecorated new version
    # this became a need after removing the reloading decorator from the newly defined version
    caller_locals_copy = caller_locals.copy()
    exec(code, caller_globals, caller_locals_copy)
    func = caller_locals_copy[fn.__name__]
    return func


_reloading_class_dict = {}


def _reloading_class(
    fn, every=1, reloadOnException=True
):  # disable the 'every' argument.
    global _reloading_class_dict
    every = 1  # override this thing. reload at every time.
    stack = inspect.stack()
    # print("stack", stack)
    # breakpoint()
    frame, fpath = stack[2][:2]
    caller_locals = frame.f_locals
    caller_globals = frame.f_globals

    # return mclass
    _reloading_class_dict[fpath] = _reloading_class_dict.get(fpath, {})
    _reloading_class_dict[fpath][fn] = _reloading_class_dict.get(fpath).get(
        fn,
        {  # this is not going to preserve the state.
            "class": None,
            "reloads": -1,
        },
    )
    # state = _reloading_class_dict[fpath][fn]

    def wrapped():
        while True:
            try:
                if reloadOnException:  # should you start a server or something?
                    if _reloading_class_dict[fpath][fn]["class"] is None:
                        _reloading_class_dict[fpath][fn][
                            "class"
                        ] = get_reloaded_function(
                            caller_globals,
                            caller_locals,
                            fpath,
                            fn,
                            funcdefs=[ast.ClassDef],
                        )
                elif _reloading_class_dict[fpath][fn]["reloads"] % every == 0:
                    _reloading_class_dict[fpath][fn]["class"] = (
                        get_reloaded_function(
                            caller_globals,
                            caller_locals,
                            fpath,
                            fn,
                            funcdefs=[ast.ClassDef],
                        )
                        or _reloading_class_dict[fpath][fn]["class"]
                    )
                    _reloading_class_dict[fpath][fn]["reloads"] += 1

                class_ = _reloading_class_dict[fpath][fn]["class"]
                # the function inside function (closure) is not handled properly. need to decorate again?
                # do not decorate already decorated function?
                return class_
            except Exception:
                while True:
                    needbreak = handle_exception(fpath)
                    if needbreak:
                        break
                    try:
                        _reloading_class_dict[fpath][fn]["class"] = (
                            get_reloaded_function(
                                caller_globals,
                                caller_locals,
                                fpath,
                                fn,
                                funcdefs=[ast.ClassDef],
                            )
                            or _reloading_class_dict[fpath][fn]["class"]
                        )
                        return _reloading_class_dict[fpath][fn]["class"]
                    except:
                        pass

    class_ = wrapped()
    caller_locals[fn.__name__] = class_
    return class_


def _reloading_function(fn, every=1, reloadOnException=True):
    stack = inspect.stack()
    # what is this stack?
    # print(stack)
    # breakpoint()
    # stack[0] -> this _reloading_function
    # stack[1] -> reloading function
    # stack[2] -> original function
    # FrameInfo(frame=<frame at 0x7f4a6346a5e0, file '/media/root/parrot/pyjom/tests/skipexception_code_and_continue_resurrection_time_travel_debugging/hook_error_handler_to_see_if_context_preserved.py', line 67, code <module>>, filename='/media/root/parrot/pyjom/tests/skipexception_code_and_continue_resurrection_time_travel_debugging/hook_error_handler_to_see_if_context_preserved.py', lineno=67, function='<module>', code_context=['def anotherFunction():\n'], index=0)
    # what the fuck is this stack[2]? let's see.
    ##########################
    # seems this is the damn function... yeah...
    # FrameInfo(frame=<frame at 0x100dfe440, file 'reload_py_template.py', line 4, code <module>>, filename='reload_py_template.py', lineno=4, function='<module>', code_context=['def testfunc():\n'], index=0)
    # mstack = stack[2]
    # print(mstack)
    # breakpoint()
    ##########################
    frame, fpath = stack[2][:2]
    caller_locals = frame.f_locals
    caller_globals = frame.f_globals
    # 'clear', 'f_back', 'f_builtins', 'f_code', 'f_globals', 'f_lasti', 'f_lineno', 'f_locals', 'f_trace', 'f_trace_lines', 'f_trace_opcodes'

    # crutch to use dict as python2 doesn't support nonlocal
    state = {
        "func": None,
        "reloads": 0,
    }

    def wrapped(*args, **kwargs):
        if reloadOnException:
            if state["func"] is None:
                state["func"] = get_reloaded_function(
                    caller_globals, caller_locals, fpath, fn
                )
        elif state["reloads"] % every == 0:
            state["func"] = (
                get_reloaded_function(caller_globals, caller_locals, fpath, fn)
                or state["func"]
            )
            state["reloads"] += 1
        while True:
            try:
                func = state["func"]
                # print(
                #     f"----\nargs:{args}\nkwargs:{kwargs}\nfunc:{func}\n----"
                # )  # try to debug.
                # the function inside function (closure) is not handled properly. need to decorate again?
                # do not decorate already decorated function?
                result = func(*args, **kwargs)
                return result
            except Exception:
                needbreak = handle_exception(fpath)
                if needbreak:
                    break
                state["func"] = (
                    get_reloaded_function(caller_globals, caller_locals, fpath, fn)
                    or state["func"]
                )

    caller_locals[fn.__name__] = wrapped
    return wrapped
