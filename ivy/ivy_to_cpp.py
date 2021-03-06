#! /usr/bin/env python
#
# Copyright (c) Microsoft Corporation. All Rights Reserved.
#

import ivy
import ivy_logic as il
import ivy_module as im
import ivy_utils as iu
import ivy_actions as ia
import logic as lg
import logic_util as lu
import ivy_solver as slv
import ivy_transrel as tr
import ivy_logic_utils as ilu
import ivy_compiler as ic
import ivy_isolate as iso
import ivy_ast
import itertools

from collections import defaultdict
from operator import mul
import re

def all_state_symbols():
    syms = il.all_symbols()
    return [s for s in syms if s not in il.sig.constructors]

def sort_card(sort):
    if hasattr(sort,'card'):
        return sort.card
    if sort.is_relational():
        return 2
    return slv.sort_card(sort)
    if hasattr(sort,'name'):
        name = sort.name
        if name in il.sig.interp:
            sort = il.sig.interp[name]
            if isinstance(sort,il.EnumeratedSort):
                return sort.card
            card = slv.sort_card(sort)
            if card != None:
                return card
    raise iu.IvyError(None,'sort {} has no finite interpretation'.format(sort))
    
indent_level = 0

def indent(header):
    header.append(indent_level * '    ')

def get_indent(line):
    lindent = 0
    for char in line:
        if char == ' ':
            lindent += 1
        elif char == '\t':
            lindent = (lindent + 8) / 8 * 8
        else:
            break
    return lindent

def indent_code(header,code):
    code = code.rstrip() # remove trailing whitespace
    nonempty_lines = [line for line in code.split('\n') if line.strip() != ""]
    indent = min(get_indent(line) for line in nonempty_lines) if nonempty_lines else 0
    for line in code.split('\n'):
        header.append((indent_level * 4 + get_indent(line) - indent) * ' ' + line.strip() + '\n')

def sym_decl(sym,c_type = None,skip_params=0,classname=None):
    name, sort = sym.name,sym.sort
    dims = []
    if not c_type:
        c_type,dims = ctype_function(sort,skip_params=skip_params,classname=classname)
    res = c_type + ' '
    res += memname(sym) if skip_params else varname(sym.name)
    for d in dims:
        res += '[' + str(d) + ']'
    return res
    
def declare_symbol(header,sym,c_type = None,skip_params=0,classname=None):
    if slv.solver_name(sym) == None:
        return # skip interpreted symbols
    header.append('    '+sym_decl(sym,c_type,skip_params,classname=classname)+';\n')

special_names = {
    '<' : '__lt',
    '<=' : '__le',
    '>' : '__gt',
    '>=' : '__ge',
}

puncs = re.compile('[\.\[\]]')

def varname(name):
    global special_names
    if not isinstance(name,str):
        name = name.name
    if name in special_names:
        return special_names[name]

    name = name.replace('loc:','loc__').replace('ext:','ext__').replace('___branch:','__branch__')
    name = re.sub(puncs,'__',name)
    return name.split(':')[-1]

def mk_nondet(code,v,rng,name,unique_id):
    global nondet_cnt
    indent(code)
    code.append(varname(v) + ' = ___ivy_choose(' + str(rng) + ',"' + name + '",' + str(unique_id) + ');\n')

def is_native_sym(sym):
    return il.is_uninterpreted_sort(sym.sort) and sym.sort.name in im.module.native_types    

def mk_nondet_sym(code,sym,name,unique_id):
    global nondet_cnt
    if is_native_sym(sym):
        return  # native classes have their own initializers
    if is_large_type(sym.sort):
        code_line(code,varname(sym) + ' = ' + make_thunk(code,variables(sym.sort.dom),HavocSymbol(sym.sort.rng,name,unique_id)))
        return
    fun = lambda v: (('___ivy_choose(' + csortcard(v.sort) + ',"' + name + '",' + str(unique_id) + ')')
                     if not is_native_sym(v) else None)
    assign_symbol_value(code,[varname(sym)],fun,sym,same=True)

def field_eq(s,t,field):
    vs = [il.Variable('X{}'.format(idx),sort) for idx,sort in enumerate(field.sort.dom[1:])]
    if not vs:
        return il.Equals(field(s),field(t))
    return il.ForAll(vs,il.Equals(field(*([s]+vs)),field(*([t]+vs))))

def memname(sym):
    return sym.name.split('.')[-1]



def ctuple(dom,classname=None):
    if len(dom) == 1:
        return ctypefull(dom[0],classname=classname)
    return (classname+'::' if classname else '') + '__tup__' + '__'.join(ctypefull(s) for s in dom)

declared_ctuples = set()

def declare_ctuple(header,dom):
    if len(dom) == 1:
        return
    t = ctuple(dom)
    if t in declared_ctuples:
        return
    declared_ctuples.add(t)
    header.append('struct ' + t + ' {\n')
    for idx,sort in enumerate(dom):
        sym = il.Symbol('arg{}'.format(idx),sort)
        declare_symbol(header,sym)
    header.append(t+'(){}')
    header.append(t+'('+','.join('const '+ctypefull(d)+' &arg'+str(idx) for idx,d in enumerate(dom))
                  + ') : '+','.join('arg'+str(idx)+'(arg'+str(idx)+')' for idx,d in enumerate(dom))
                  + '{}\n')
    header.append("        size_t __hash() const { return "+struct_hash_fun(['arg{}'.format(n) for n in range(len(dom))],dom) + ";}\n")
    header.append('};\n')

def ctuple_hash(dom):
    if len(dom) == 1:
        return 'hash<'+ctypefull(dom[0])+'>'
    else:
        return 'hash__' + ctuple(dom)

def declare_ctuple_hash(header,dom,classname):
    t = ctuple(dom)
    the_type = classname+'::'+t
    header.append("""
class the_hash_type {
    public:
        size_t operator()(const the_type &__s) const {
            return the_val;
        }
    };
""".replace('the_hash_type',ctuple_hash(dom)).replace('the_type',the_type).replace('the_val','+'.join('hash_space::hash<{}>()(__s.arg{})'.format(ctype(s),i,classname=classname) for i,s in enumerate(dom))))

                  
def declare_hash_thunk(header):
    header.append("""
template <typename D, typename R>
struct thunk {
    virtual R operator()(const D &) = 0;
    int ___ivy_choose(int rng,const char *name,int id) {
        return 0;
    }
};
template <typename D, typename R, class HashFun = hash_space::hash<D> >
struct hash_thunk {
    thunk<D,R> *fun;
    hash_space::hash_map<D,R,HashFun> memo;
    hash_thunk() : fun(0) {}
    hash_thunk(thunk<D,R> *fun) : fun(fun) {}
    ~hash_thunk() {
//        if (fun)
//            delete fun;
    }
    R &operator[](const D& arg){
        std::pair<typename hash_space::hash_map<D,R>::iterator,bool> foo = memo.insert(std::pair<D,R>(arg,R()));
        R &res = foo.first->second;
        if (foo.second)
            res = (*fun)(arg);
        return res;
    }
};
""")        

def all_members():
    for sym in il.all_symbols():
        if sym_is_member(sym) and not slv.solver_name(sym) == None:
            yield sym

def all_ctuples():
    done = set()
    for sym in all_members():
        if hasattr(sym.sort,'dom') and len(sym.sort.dom) > 1 and is_large_type(sym.sort):
            res = tuple(sym.sort.dom)
            if res not in done:
                done.add(res)
                yield res
    
def declare_all_ctuples(header):
    for dom in all_ctuples():
        declare_ctuple(header,dom)

def declare_all_ctuples_hash(header,classname):
    for dom in all_ctuples():
        declare_ctuple_hash(header,dom,classname)

def ctype(sort,classname=None):
    if il.is_uninterpreted_sort(sort):
        if sort.name in im.module.native_types or sort.name in im.module.sort_destructors:
            return ((classname+'::') if classname != None else '') + varname(sort.name)
    return 'bool' if sort.is_relational() else 'int'
    
def ctypefull(sort,classname=None):
    if il.is_uninterpreted_sort(sort):
        if sort.name in im.module.native_types:
            return native_type_full(im.module.native_types[sort.name])
        if sort.name in im.module.sort_destructors:
            return ((classname+'::') if classname != None else '') + varname(sort.name)
    return 'bool' if sort.is_relational() else 'int'

def native_type_full(self):
    return self.args[0].inst(native_reference,self.args[1:])    

def is_large_type(sort):
    cards = map(sort_card,sort.dom if hasattr(sort,'dom') else [])
    return not(all(cards) and reduce(mul,cards,1) <= 16)

def ctype_function(sort,classname=None,skip_params=0):
    cards = map(sort_card,sort.dom[skip_params:] if hasattr(sort,'dom') else [])
    cty = ctypefull(sort.rng,classname)
    if all(cards) and reduce(mul,cards,1) <= 16:
        return (cty,cards)
    cty = 'hash_thunk<'+ctuple(sort.dom)+','+cty+'>'
    return (cty,[])
    
native_expr_full = native_type_full

thunk_counter = 0


def expr_to_z3(expr):
    fmla = '(assert ' + slv.formula_to_z3(expr).sexpr().replace('\n',' ') + ')'
    return 'z3::expr(g.ctx,Z3_parse_smtlib2_string(ctx, "{}", sort_names.size(), &sort_names[0], &sorts[0], decl_names.size(), &decl_names[0], &decls[0]))'.format(fmla)



def make_thunk(impl,vs,expr):
    global the_classname
    dom = [v.sort for v in vs]
    D = ctuple(dom,classname=the_classname)
    R = ctypefull(expr.sort)
    global thunk_counter
    name = '__thunk__{}'.format(thunk_counter)
    thunk_counter += 1
    thunk_class = 'z3_thunk' if target.get() in ["gen","test"] else 'thunk'
    open_scope(impl,line='struct {} : {}<{},{}>'.format(name,thunk_class,D,R))
    env = list(ilu.used_symbols_ast(expr))
    for sym in env:
        declare_symbol(impl,sym)
    envnames = [varname(sym) for sym in env]
    open_scope(impl,line='{}({}) {} {}'.format(name,','.join(sym_decl(sym) for sym in env)
                                             ,':' if envnames else ''
                                             ,','.join('{}({})'.format(n,n) for n in envnames))),
    close_scope(impl)
    open_scope(impl,line='{} operator()(const {} &arg)'.format(R,D))
    subst = {vs[0].name:il.Symbol('arg',vs[0].sort)} if len(vs)==1 else dict((v.name,il.Symbol('arg.arg{}'.format(idx),v.sort)) for idx,v in enumerate(vs))
    expr = ilu.substitute_ast(expr,subst)
    code_line(impl,'return ' + code_eval(impl,expr))
    close_scope(impl)
    if target.get() in ["gen","test"]:
        open_scope(impl,line = 'z3::expr to_z3(gen &g, const z3::expr &v)')
        if lu.free_variables(expr):
            raise iu.IvyError(None,"cannot compile {}".format(expr))
        if all(s.is_numeral() for s in ilu.used_symbols_ast(expr)):
            code_line(impl,'z3::expr res = v == g.int_to_z3(g.sort("{}"),(int)({}))'.format(expr.sort.name,code_eval(impl,expr)))
        else:
            raise iu.IvyError(None,"cannot compile {}".format(expr))
        code_line(impl,'return res')
        close_scope(impl)
    close_scope(impl,semi=True)
    return 'hash_thunk<{},{}>(new {}({}))'.format(D,R,name,','.join(envnames))

def struct_hash_fun(field_names,field_sorts):
    return '+'.join('hash_space::hash<{}>()({})'.format(ctype(s),varname(f)) for s,f in zip(field_sorts,field_names))

def emit_struct_hash(header,the_type,field_names,field_sorts):
    header.append("""
    template<> class hash<the_type> {
        public:
            size_t operator()(const the_type &__s) const {
                return the_val;
             }
    };
""".replace('the_type',the_type).replace('the_val',struct_hash_fun(['__s.'+n for n in field_names],field_sorts)))

def emit_cpp_sorts(header):
    for name in im.module.sort_order:
        if name in im.module.native_types:
            nt = native_type_full(im.module.native_types[name])
            header.append("    typedef " + nt + ' ' + varname(name) + ";\n");
        elif name in im.module.sort_destructors:
            header.append("    struct " + varname(name) + " {\n");
            destrs = im.module.sort_destructors[name]
            for destr in destrs:
                declare_symbol(header,destr,skip_params=1)
            header.append("        size_t __hash() const { return "+struct_hash_fun(map(memname,destrs),[d.sort.rng for d in destrs]) + ";}\n")
            header.append("    };\n");
            

def emit_sorts(header):
    for name,sort in il.sig.sorts.iteritems():
        if name == "bool":
            continue
        if name in il.sig.interp:
            sort = il.sig.interp[name]
        if not isinstance(sort,il.EnumeratedSort):
            sortname = str(sort)
#            print "sortname: {}".format(sortname)
            if sortname.startswith('bv[') and sortname.endswith(']'):
                width = int(sortname[3:-1])
                indent(header)
                header.append('mk_bv("{}",{});\n'.format(name,width))
                continue
            header.append('mk_sort("{}");\n'.format(name))
            continue
#            raise iu.IvyError(None,'sort {} has no finite interpretation'.format(name))
        card = sort.card
        cname = varname(name)
        indent(header)
        header.append("const char *{}_values[{}]".format(cname,card) +
                      " = {" + ','.join('"{}"'.format(x) for x in sort.extension) + "};\n");
        indent(header)
        header.append('mk_enum("{}",{},{}_values);\n'.format(name,card,cname))

def emit_decl(header,symbol):
    name = symbol.name
    sname = slv.solver_name(symbol)
    if sname == None:  # this means the symbol is interpreted in some theory
        return 
    cname = varname(name)
    sort = symbol.sort
    rng_name = "Bool" if sort.is_relational() else sort.rng.name
    domain = sort_domain(sort)
    if len(domain) == 0:
        indent(header)
        header.append('mk_const("{}","{}");\n'.format(sname,rng_name))
    else:
        card = len(domain)
        indent(header)
        header.append("const char *{}_domain[{}]".format(cname,card) + " = {"
                      + ','.join('"{}"'.format(s.name) for s in domain) + "};\n");
        indent(header)
        header.append('mk_decl("{}",{},{}_domain,"{}");\n'.format(sname,card,cname,rng_name))
        
def emit_sig(header):
    emit_sorts(header)
    for symbol in all_state_symbols():
        emit_decl(header,symbol)

def sort_domain(sort):
    if hasattr(sort,"domain"):
        return sort.domain
    return []

def emit_eval(header,symbol,obj=None,classname=None): 
    global indent_level
    name = symbol.name
    sname = slv.solver_name(symbol)
    cname = varname(name)
    sort = symbol.sort
    domain = sort_domain(sort)
    for idx,dsort in enumerate(domain):
        dcard = sort_card(dsort)
        indent(header)
        header.append("for (int X{} = 0; X{} < {}; X{}++)\n".format(idx,idx,dcard,idx))
        indent_level += 1
    indent(header)
    if sort.rng.name in im.module.sort_destructors:
        code_line(header,'__from_solver<'+classname+'::'+varname(sort.rng.name)+'>(*this,apply("'+symbol.name+'"'+''.join(',int_to_z3(sort("'+s.name+'"),X{}'.format(idx)+')' for idx,s in enumerate(domain))+'),'+varname(symbol)+''.join('[X{}]'.format(idx) for idx in range(len(domain)))+')')
    else:
        header.append((obj + '.' if obj else '')
                      + cname + ''.join("[X{}]".format(idx) for idx in range(len(domain)))
                      + ' = eval_apply("{}"'.format(sname)
                      + ''.join(",X{}".format(idx) for idx in range(len(domain)))
                      + ");\n")
    for idx,dsort in enumerate(domain):
        indent_level -= 1    

def emit_set_field(header,symbol,lhs,rhs,nvars=0):
    global indent_level
    name = symbol.name
    sname = slv.solver_name(symbol)
    cname = varname(name)
    sort = symbol.sort
    domain = sort.dom[1:]
    vs = variables(domain,start=nvars)
    open_loop(header,vs)
    lhs1 = 'apply("'+symbol.name+'"'+''.join(','+s for s in ([lhs]+map(varname,vs))) + ')'
    rhs1 = rhs + ''.join('[{}]'.format(varname(v)) for v in vs) + '.' + varname(symbol)
    if sort.rng.name in im.module.sort_destructors:
        destrs = im.module.sort_destructors[sort.name]
        for destr in destrs:
            emit_set_field(header,destr,lhs1,rhs1,nvars+len(vs))
    else:
        code_line(header,'slvr.add('+lhs1+'==int_to_z3(enum_sorts.find("'+sort.rng.name+'")->second,'+rhs1+'))')
    close_loop(header,vs)


def emit_set(header,symbol): 
    global indent_level
    name = symbol.name
    sname = slv.solver_name(symbol)
    cname = varname(name)
    sort = symbol.sort
    domain = sort_domain(sort)
    if sort.rng.name in im.module.sort_destructors:
        destrs = im.module.sort_destructors[sort.name]
        for destr in destrs:
            vs = variables(domain)
            open_loop(header,vs)
            lhs = 'apply("'+symbol.name+'"'+''.join(','+s for s in map(varname,vs)) + ')'
            rhs = 'obj.' + varname(symbol) + ''.join('[{}]'.format(varname(v)) for v in vs)
            emit_set_field(header,destr,lhs,rhs,len(vs))
            close_loop(header,vs)
        return
    if is_large_type(sort):
        vs = variables(sort.dom)
        cvars = ','.join('ctx.constant("{}",sort("{}"))'.format(varname(v),v.sort.name) for v in vs)
        code_line(header,'slvr.add(forall({},__to_solver(*this,apply("{}",{}),obj.{})))'.format(cvars,sname,cvars,cname))
        return
    for idx,dsort in enumerate(domain):
        dcard = sort_card(dsort)
        indent(header)
        header.append("for (int X{} = 0; X{} < {}; X{}++)\n".format(idx,idx,dcard,idx))
        indent_level += 1
    indent(header)
    header.append('set("{}"'.format(sname)
                  + ''.join(",X{}".format(idx) for idx in range(len(domain)))
                  + ",obj.{}".format(cname)+ ''.join("[X{}]".format(idx) for idx in range(len(domain)))
                  + ");\n")
    for idx,dsort in enumerate(domain):
        indent_level -= 1    

def sym_is_member(sym):
    global is_derived
    res = sym not in is_derived and sym.name not in im.module.destructor_sorts
    return res

def emit_eval_sig(header,obj=None,used=None,classname=None):
    for symbol in all_state_symbols():
        if slv.solver_name(symbol) != None: # skip interpreted symbols
            global is_derived
            if symbol not in is_derived:
                if used == None or symbol in used:
                    emit_eval(header,symbol,obj,classname=classname)

def emit_clear_progress(impl,obj=None):
    for df in im.module.progress:
        vs = list(lu.free_variables(df.args[0]))
        open_loop(impl,vs)
        code = []
        indent(code)
        if obj != None:
            code.append('obj.')
        df.args[0].emit(impl,code)
        code.append(' = 0;\n')
        impl.extend(code)
        close_loop(impl,vs)

def mk_rand(sort):
    card = csortcard(sort)
    return '(rand() % {})'.format(card) if card else 0

def emit_init_gen(header,impl,classname):
    global indent_level
    header.append("""
class init_gen : public gen {
public:
    init_gen();
""")
    header.append("    bool generate(" + classname + "&);\n")
    header.append("    bool execute(" + classname + "&){}\n};\n")
    impl.append("init_gen::init_gen(){\n");
    indent_level += 1
    emit_sig(impl)
    indent(impl)
    impl.append('add("(assert (and\\\n')
    constraints = [im.module.init_cond.to_formula()]
    for a in im.module.axioms:
        constraints.append(a)
    for ldf in im.module.definitions:
        constraints.append(ldf.formula.to_constraint())
    for c in constraints:
        fmla = slv.formula_to_z3(c).sexpr().replace('\n',' ')
        indent(impl)
        impl.append("  {}\\\n".format(fmla))
    indent(impl)
    impl.append('))");\n')
    indent_level -= 1
    impl.append("}\n");
    used = ilu.used_symbols_asts(constraints)
    impl.append("bool init_gen::generate(" + classname + "& obj) {\n")
    indent_level += 1
    for sym in all_state_symbols():
        if slv.solver_name(il.normalize_symbol(sym)) != None: # skip interpreted symbols
            global is_derived
            if sym_is_member(sym):
                if sym in used:
                    emit_randomize(impl,sym,classname=classname)
                else:
                    if not is_native_sym(sym) and not is_large_type(sym.sort):
                        fun = lambda v: (mk_rand(v.sort) if not is_native_sym(v) else None)
                        assign_array_from_model(impl,sym,'obj.',fun)
    indent_level -= 1
    impl.append("""
    // std::cout << slvr << std::endl;
    bool __res = solve();
    if (__res) {
""")
    indent_level += 2
    emit_eval_sig(impl,'obj',used = used,classname=classname)
    emit_clear_progress(impl,'obj')
    indent_level -= 2
    impl.append("""
    }
""")
    impl.append("""
    obj.__init();
    return __res;
}
""")
    
def emit_randomize(header,symbol,classname=None):

    global indent_level
    name = symbol.name
    sname = slv.solver_name(symbol)
    cname = varname(name)
    sort = symbol.sort
    domain = sort_domain(sort)
    for idx,dsort in enumerate(domain):
        dcard = sort_card(dsort)
        indent(header)
        header.append("for (int X{} = 0; X{} < {}; X{}++)\n".format(idx,idx,dcard,idx))
        indent_level += 1
    if sort.rng.name in im.module.sort_destructors:
        code_line(header,'__randomize<'+classname+'::'+varname(sort.rng.name)+'>(*this,apply("'+symbol.name+'"'+''.join(',int_to_z3(sort("'+s.name+'"),X{}'.format(idx)+')' for idx,s in enumerate(domain))+'))')
    else:
        indent(header)
        header.append('randomize("{}"'.format(sname)
                      + ''.join(",X{}".format(idx) for idx in range(len(domain)))
                      + ");\n")
    for idx,dsort in enumerate(domain):
        indent_level -= 1    

#    indent(header)
#    header.append('randomize("{}");\n'.format(slv.solver_name(symbol)))


def is_local_sym(sym):
    sym = il.normalize_symbol(sym)
    return not il.sig.contains_symbol(sym) and slv.solver_name(il.normalize_symbol(sym)) != None

def emit_action_gen(header,impl,name,action,classname):
    global indent_level
    caname = varname(name)
    upd = action.update(im.module,None)
    pre = tr.reverse_image(ilu.true_clauses(),ilu.true_clauses(),upd)
    pre_clauses = ilu.trim_clauses(pre)
    pre_clauses = ilu.and_clauses(pre_clauses,ilu.Clauses([ldf.formula.to_constraint() for ldf in im.module.definitions]))
    pre = pre_clauses.to_formula()
    used = set(ilu.used_symbols_ast(pre))
    used_names = set(varname(s) for s in used)
    for p in action.formal_params:
        if varname(p) not in used_names:
            used.add(p)
    syms = [x for x in used if is_local_sym(x) and not x.is_numeral()]
    header.append("class " + caname + "_gen : public gen {\n  public:\n")
    for sym in syms:
        if not sym.name.startswith('__ts') and sym not in pre_clauses.defidx:
            declare_symbol(header,sym,classname=classname)
    header.append("    {}_gen();\n".format(caname))
    header.append("    bool generate(" + classname + "&);\n");
    header.append("    bool execute(" + classname + "&);\n};\n");
    impl.append(caname + "_gen::" + caname + "_gen(){\n");
    indent_level += 1
    emit_sig(impl)
    for sym in syms:
        emit_decl(impl,sym)
    
    indent(impl)
    impl.append('add("(assert {})");\n'.format(slv.formula_to_z3(pre).sexpr().replace('\n','\\\n')))
    indent_level -= 1
    impl.append("}\n");
    impl.append("bool " + caname + "_gen::generate(" + classname + "& obj) {\n    push();\n")
    indent_level += 1
    pre_used = ilu.used_symbols_ast(pre)
    for sym in all_state_symbols():
        if sym in pre_used and sym not in pre_clauses.defidx: # skip symbols not used in constraint
            if slv.solver_name(il.normalize_symbol(sym)) != None: # skip interpreted symbols
                if sym_is_member(sym):
                    emit_set(impl,sym)
    for sym in syms:
        if not sym.name.startswith('__ts') and sym not in pre_clauses.defidx:
            emit_randomize(impl,sym,classname=classname)
    impl.append("""
    // std::cout << slvr << std::endl;
    bool __res = solve();
    if (__res) {
""")
    indent_level += 1
    for sym in syms:
        if not sym.name.startswith('__ts') and sym not in pre_clauses.defidx:
            emit_eval(impl,sym,classname=classname)
    indent_level -= 2
    impl.append("""
    }
    pop();
    obj.___ivy_gen = this;
    return __res;
}
""")
    open_scope(impl,line="bool " + caname + "_gen::execute(" + classname + "& obj)")
    if action.formal_params:
        code_line(impl,'std::cout << "> {}("'.format(name.split(':')[-1]) + ' << "," '.join(' << {}'.format(varname(p)) for p in action.formal_params) + ' << ")" << std::endl')
    else:
        code_line(impl,'std::cout << "> {}"'.format(name.split(':')[-1]) + ' << std::endl')
    call = 'obj.{}('.format(caname) + ','.join(varname(p) for p in action.formal_params) + ')'
    if len(action.formal_returns) == 0:
        code_line(impl,call)
    else:
        code_line(impl,'std::cout << "= " << ' + call)
    close_scope(impl)


def emit_derived(header,impl,df,classname):
    name = df.defines().name
    sort = df.defines().sort.rng
    retval = il.Symbol("ret:val",sort)
    vs = df.args[0].args
    ps = [ilu.var_to_skolem('p:',v) for v in vs]
    mp = dict(zip(vs,ps))
    rhs = ilu.substitute_ast(df.args[1],mp)
    action = ia.AssignAction(retval,rhs)
    action.formal_params = ps
    action.formal_returns = [retval]
    emit_some_action(header,impl,name,action,classname)


def native_split(string):
    split = string.split('\n',1)
    if len(split) == 2:
        tag = split[0].strip()
        return ("member" if not tag else tag),split[1]
    return "member",split[0]

def native_type(native):
    tag,code = native_split(native.args[1].code)
    return tag

def native_declaration(atom):
    if atom.rep in im.module.sig.sorts:
        return ctype(im.module.sig.sorts[atom.rep],classname=native_classname)
    res = varname(atom.rep)
    for arg in atom.args:
        sort = arg.sort if isinstance(arg.sort,str) else arg.sort.name
        res += '[' + str(sort_card(im.module.sig.sorts[sort])) + ']'
    return res

thunk_counter = 0

def action_return_type(action):
    return ctype(action.formal_returns[0].sort) if action.formal_returns else 'void'

def thunk_name(actname):
    return 'thunk__' + varname(actname)

def create_thunk(impl,actname,action,classname):
    tc = thunk_name(actname)
    impl.append('struct ' + tc + '{\n')
    impl.append('    ' + classname + ' *__ivy' + ';\n')
    
    params = [p for p in action.formal_params if p.name.startswith('prm:')]
    inputs = [p for p in action.formal_params if not p.name.startswith('prm:')]
    for p in params:
        declare_symbol(impl,p)
    impl.append('    ')
    emit_param_decls(impl,tc,params,extra = [ classname + ' *__ivy'],classname=classname)
    impl.append(': __ivy(__ivy)' + ''.join(',' + varname(p) + '(' + varname(p) + ')' for p in params) + '{}\n')
    impl.append('    ' + action_return_type(action) + ' ')
    emit_param_decls(impl,'operator()',inputs,classname=classname);
    impl.append(' const {\n        __ivy->' + varname(actname)
                + '(' + ','.join(varname(p.name) for p in action.formal_params) + ');\n    }\n};\n')

def native_typeof(arg):
    if isinstance(arg,ivy_ast.Atom):
        if arg.rep in im.module.actions:
            return thunk_name(arg.rep)
        raise iu.IvyError(arg,'undefined action: ' + arg.rep)
    return int + len(arg.sort.dom) * '[]'


def native_to_str(native,reference=False):
    tag,code = native_split(native.args[1].code)
    fields = code.split('`')
    f = native_reference if reference else native_declaration
    def nfun(idx):
        return native_typeof if fields[idx-1].endswith('%') else f
    def dm(s):
        return s[:-1] if s.endswith('%') else s
    fields = [(nfun(idx)(native.args[int(s)+2]) if idx % 2 == 1 else dm(s)) for idx,s in enumerate(fields)]
    return ''.join(fields)

def emit_native(header,impl,native,classname):
    header.append(native_to_str(native))

def emit_param_decls(header,name,params,extra=[],classname=None):
    header.append(varname(name) + '(')
    header.append(', '.join(extra + [ctype(p.sort,classname=classname) + ' ' + varname(p.name) for p in params]))
    header.append(')')

def emit_method_decl(header,name,action,body=False,classname=None):
    if not hasattr(action,"formal_returns"):
        print "bad name: {}".format(name)
        print "bad action: {}".format(action)
    rs = action.formal_returns
    if not body:
        header.append('    ')
    if not body and target.get() != "gen":
        header.append('virtual ')
    if len(rs) == 0:
        header.append('void ')
    elif len(rs) == 1:
        header.append(ctype(rs[0].sort,classname=classname) + ' ')
    else:
        raise iu.IvyError(action,'cannot handle multiple output values')
    if body:
        header.append(classname + '::')
    emit_param_decls(header,name,action.formal_params)
    
def emit_action(header,impl,name,classname):
    action = im.module.actions[name]
    emit_some_action(header,impl,name,action,classname)

def emit_some_action(header,impl,name,action,classname):
    global indent_level
    emit_method_decl(header,name,action)
    header.append(';\n')
    global thunks
    thunks = impl
    code = []
    emit_method_decl(code,name,action,body=True,classname=classname)
    code.append('{\n')
    indent_level += 1
    if len(action.formal_returns) == 1:
        indent(code)
        p = action.formal_returns[0]
        if p not in action.formal_params:
            code.append(ctype(p.sort) + ' ' + varname(p.name) + ';\n')
            mk_nondet_sym(code,p,p.name,0)
    with ivy_ast.ASTContext(action):
        action.emit(code)
    if len(action.formal_returns) == 1:
        indent(code)
        code.append('return ' + varname(action.formal_returns[0].name) + ';\n')
    indent_level -= 1
    code.append('}\n')
    impl.extend(code)

def init_method():
    asserts = []
    for ini in im.module.labeled_inits + im.module.labeled_axioms:
        act = ia.AssertAction(ini.formula)
        act.lineno = ini.lineno
        asserts.append(act)
    
    for name,ini in im.module.initializers:
        asserts.append(ini)

    res = ia.Sequence(*asserts)
    res.formal_params = []
    res.formal_returns = []
    return res

def check_iterable_sort(sort):
    if ctype(sort) not in ["bool","int"]:
        raise iu.IvyError(None,"cannot iterate over non-integer sort {}".format(sort))
    

def open_loop(impl,vs,declare=True):
    global indent_level
    for idx in vs:
        check_iterable_sort(idx.sort)
        indent(impl)
        impl.append('for ('+ ('int ' if declare else '') + idx.name + ' = 0; ' + idx.name + ' < ' + str(sort_card(idx.sort)) + '; ' + idx.name + '++) {\n')
        indent_level += 1

def close_loop(impl,vs):
    global indent_level
    for idx in vs:
        indent_level -= 1    
        indent(impl)
        impl.append('}\n')
        
def open_scope(impl,newline=False,line=None):
    global indent_level
    if line != None:
        indent(impl)
        impl.append(line)
    if newline:
        impl.append('\n')
        indent(impl)
    impl.append('{\n')
    indent_level += 1

def open_if(impl,cond):
    open_scope(impl,line='if('+(''.join(cond) if isinstance(cond,list) else cond)+')')
    
def close_scope(impl,semi=False):
    global indent_level
    indent_level -= 1
    indent(impl)
    impl.append('}'+(';' if semi else '')+'\n')

# This generates the "tick" method, called by the test environment to
# represent passage of time. For each progress property, if it is not
# satisfied the counter is incremented else it is set to zero. For each
# property the maximum of the counter values for all its relies is
# computed and the test environment's ivy_check_progress function is called.

# This is currently a bit bogus, since we could miss satisfaction of
# the progress property occurring between ticks.

def emit_tick(header,impl,classname):
    global indent_level
    indent_level += 1
    indent(header)
    header.append('void __tick(int timeout);\n')
    indent_level -= 1
    indent(impl)
    impl.append('void ' + classname + '::__tick(int __timeout){\n')
    indent_level += 1

    rely_map = defaultdict(list)
    for df in im.module.rely:
        key = df.args[0] if isinstance(df,il.Implies) else df
        rely_map[key.rep].append(df)

    for df in im.module.progress:
        vs = list(lu.free_variables(df.args[0]))
        open_loop(impl,vs)
        code = []
        indent(code)
        df.args[0].emit(impl,code)
        code.append(' = ')
        df.args[1].emit(impl,code)
        code.append(' ? 0 : ')
        df.args[0].emit(impl,code)
        code.append(' + 1;\n')
        impl.extend(code)
        close_loop(impl,vs)


    for df in im.module.progress:
        if any(not isinstance(r,il.Implies) for r in rely_map[df.defines()]):
            continue
        vs = list(lu.free_variables(df.args[0]))
        open_loop(impl,vs)
        maxt = new_temp(impl)
        indent(impl)
        impl.append(maxt + ' = 0;\n') 
        for r in rely_map[df.defines()]:
            if not isinstance(r,il.Implies):
                continue
            rvs = list(lu.free_variables(r.args[0]))
            assert len(rvs) == len(vs)
            subs = dict(zip(rvs,vs))

            ## TRICKY: If there are any free variables on rhs of
            ## rely not occuring on left, we must prevent their capture
            ## by substitution

            xvs = set(lu.free_variables(r.args[1]))
            xvs = xvs - set(rvs)
            for xv in xvs:
                subs[xv.name] = xv.rename(xv.name + '__')
            xvs = [subs[xv.name] for xv in xvs]
    
            e = ilu.substitute_ast(r.args[1],subs)
            open_loop(impl,xvs)
            indent(impl)
            impl.append('{} = std::max({},'.format(maxt,maxt))
            e.emit(impl,impl)
            impl.append(');\n')
            close_loop(impl,xvs)
        indent(impl)
        impl.append('if (' + maxt + ' > __timeout)\n    ')
        indent(impl)
        df.args[0].emit(impl,impl)
        impl.append(' = 0;\n')
        indent(impl)
        impl.append('ivy_check_progress(')
        df.args[0].emit(impl,impl)
        impl.append(',{});\n'.format(maxt))
        close_loop(impl,vs)

    indent_level -= 1
    indent(impl)
    impl.append('}\n')

def csortcard(s):
    card = sort_card(s)
    return str(card) if card else "0"

def check_member_names(classname):
    names = map(varname,(list(il.sig.symbols) + list(il.sig.sorts) + list(im.module.actions)))
    if classname in names:
        raise iu.IvyError(None,'Cannot create C++ class {} with member {}.\nUse command line option classname=... to change the class name.'
                          .format(classname,classname))

def emit_ctuple_to_solver(header,dom,classname):
    ct_name = classname + '::' + ctuple(dom)
    ch_name = classname + '::' + ctuple_hash(dom)
    open_scope(header,line='template<typename R> class to_solver_class<hash_thunk<D,R> >'.replace('D',ct_name).replace('H',ch_name))
    code_line(header,'public:')
    open_scope(header,line='z3::expr operator()( gen &g, const  z3::expr &v, hash_thunk<D,R> &val)'.replace('D',ct_name).replace('H',ch_name))
    code_line(header,'z3::expr res = g.ctx.bool_val(true)')
    code_line(header,'z3::expr disj = g.ctx.bool_val(false)')
    open_scope(header,line='for(typename hash_map<D,R>::iterator it=val.memo.begin(), en = val.memo.end(); it != en; it++)'.replace('D',ct_name).replace('H',ch_name))
    code_line(header,'z3::expr cond = '+' && '.join('__to_solver(g,v.arg('+str(n)+'),it->first.arg'+str(n)+')' for n in range(len(dom))))
    code_line(header,'res = res && implies(cond,__to_solver(g,v,it->second))')
    code_line(header,'disj = disj || cond')
    close_scope(header)
    code_line(header,'res = res && (disj || dynamic_cast<z3_thunk<D,R> *>(val.fun)->to_z3(g,v))'.replace('D',ct_name))
    code_line(header,'return res')
    close_scope(header)
    close_scope(header,semi=True)

def emit_all_ctuples_to_solver(header,classname):
    for dom in all_ctuples():
        emit_ctuple_to_solver(header,dom,classname)

def emit_ctuple_equality(header,dom,classname):
    t = ctuple(dom)
    open_scope(header,line = 'bool operator==(const {}::{} &x, const {}::{} &y)'.format(classname,t,classname,t))
    code_line(header,'return '+' && '.join('x.arg{} == y.arg{}'.format(n,n) for n in range(len(dom))))
    close_scope(header)


def module_to_cpp_class(classname,basename):
    global the_classname
    the_classname = classname
    check_member_names(classname)
    global is_derived
    is_derived = set()
    for ldf in im.module.definitions:
        is_derived.add(ldf.formula.defines())

    # remove the actions not reachable from exported
        
# TODO: may want to call internal actions from testbench

#    ra = iu.reachable(im.module.public_actions,lambda name: im.module.actions[name].iter_calls())
#    im.module.actions = dict((name,act) for name,act in im.module.actions.iteritems() if name in ra)

    header = []
    if target.get() == "gen":
        header.append('extern void ivy_assert(bool,const char *);\n')
        header.append('extern void ivy_assume(bool,const char *);\n')
        header.append('extern void ivy_check_progress(int,int);\n')
        header.append('extern int choose(int,int);\n')
    if target.get() in ["gen","test"]:
        header.append('struct ivy_gen {virtual int choose(int rng,const char *name) = 0;};\n')
#    header.append('#include <vector>\n')

    header.append(hash_h)

    declare_hash_thunk(header)

    once_memo = set()
    for native in im.module.natives:
        tag = native_type(native)
        if tag == "header":
            code = native_to_str(native)
            if code not in once_memo:
                once_memo.add(code)
                header.append(code)


    header.append('class ' + classname + ' {\n  public:\n')
    header.append('    std::vector<int> ___ivy_stack;\n')
    if target.get() in ["gen","test"]:
        header.append('    ivy_gen *___ivy_gen;\n')
    header.append('    int ___ivy_choose(int rng,const char *name,int id);\n')
    if target.get() != "gen":
        header.append('    virtual void ivy_assert(bool,const char *){}\n')
        header.append('    virtual void ivy_assume(bool,const char *){}\n')
        header.append('    virtual void ivy_check_progress(int,int){}\n')
    
    impl = ['#include "' + basename + '.h"\n\n']
    impl.append("#include <sstream>\n")
    impl.append("#include <algorithm>\n")
    impl.append("""
#include <iostream>
#include <stdlib.h>
#include <sys/types.h>          /* See NOTES */
#include <sys/socket.h>
#include <netinet/in.h>
#include <netinet/ip.h> 
#include <sys/select.h>
#include <string.h>
#include <stdio.h>
#include <string>
#include <unistd.h>
""")
    impl.append("typedef {} ivy_class;\n".format(classname))

    native_exprs = []
    for n in im.module.natives:
        native_exprs.extend(n.args[2:])
    for n in im.module.actions.values():
        if isinstance(n,ia.NativeAction):
            native_exprs.extend(n.args[1:])
    callbacks = set()
    for e in native_exprs:
        if isinstance(e,ivy_ast.Atom) and e.rep in im.module.actions:
            callbacks.add(e.rep)
    for actname in sorted(callbacks):
        action = im.module.actions[actname]
        create_thunk(impl,actname,action,classname)

    if target.get() in ["test"]:
        sf = header if target.get() == "gen" else impl
        emit_boilerplate1(sf,impl,classname)

    impl.append("""
class reader {
public:
    virtual int fdes() = 0;
    virtual void read() = 0;
};
void install_reader(reader *);
class timer {
public:
    virtual int ms_delay() = 0;
    virtual void timeout(int) = 0;
};
void install_timer(timer *);
struct ivy_value {
    std::string atom;
    std::vector<ivy_value> fields;
    bool is_member() const {
        return atom.size() && fields.size();
    }
};
struct out_of_bounds {
    int idx;
    out_of_bounds(int _idx) : idx(_idx) {}
};

template <class T> T _arg(std::vector<ivy_value> &args, unsigned idx, int bound);

template <>
int _arg<int>(std::vector<ivy_value> &args, unsigned idx, int bound) {
    int res = atoi(args[idx].atom.c_str());
    if (bound && (res < 0 || res >= bound) || args[idx].fields.size())
        throw out_of_bounds(idx);
    return res;
}

template <class T> void __ser(std::vector<char> &res, const T &inp);

template <>
void __ser<int>(std::vector<char> &res, const int &inp) {
    for (int i = sizeof(int)-1; i >= 0 ; i--)
        res.push_back((inp>>(8*i))&0xff);
}

template <>
void __ser<bool>(std::vector<char> &res, const bool &inp) {
        res.push_back(inp);
}

struct deser_err {
};

template <class T> void __deser(const std::vector<char> &inp, unsigned &pos, T &res);

template <>
void __deser<int>(const std::vector<char> &inp, unsigned &pos, int &res) {
    if (inp.size() < pos + sizeof(int))
        throw deser_err();
    res = 0;
    for (int i = 0; i < sizeof(int); i++)
        res = (res << 8) | (((int)inp[pos++]) & 0xff);
}

template <>
void __deser<bool>(const std::vector<char> &inp, unsigned &pos, bool &res) {
    if (inp.size() < pos + 1)
        throw deser_err();
    res = inp[pos++] ? true : false;
}

class gen;

""")
    if target.get() in ["gen","test"]:
        impl.append("""
template <class T> void __from_solver( gen &g, const  z3::expr &v, T &res);

template <>
void __from_solver<int>( gen &g, const  z3::expr &v, int &res) {
    res = g.eval(v);
}

template <>
void __from_solver<bool>( gen &g, const  z3::expr &v, bool &res) {
    res = g.eval(v);
}

template <class T>
class to_solver_class {
};

template <class T> z3::expr __to_solver( gen &g, const  z3::expr &v, T &val) {
    return to_solver_class<T>()(g,v,val);
}


template <>
z3::expr __to_solver<int>( gen &g, const  z3::expr &v, int &val) {
    return v == g.int_to_z3(v.get_sort(),val);
}

template <>
z3::expr __to_solver<bool>( gen &g, const  z3::expr &v, bool &val) {
    return v == g.int_to_z3(v.get_sort(),val);
}

template <class T> void __randomize( gen &g, const  z3::expr &v);

template <>
void __randomize<int>( gen &g, const  z3::expr &v) {
    g.randomize(v);
}

template <>
void __randomize<bool>( gen &g, const  z3::expr &v) {
    g.randomize(v);
}

template<typename D, typename R>
class z3_thunk : public thunk<D,R> {
    public:
       virtual z3::expr to_z3(gen &g, const  z3::expr &v) = 0;
};

""")

    if True or target.get() == "repl":
        for sort_name in sorted(im.module.sort_destructors):
            csname = varname(sort_name)
            cfsname = classname + '::' + csname
            impl.append('std::ostream &operator <<(std::ostream &s, const {} &t);\n'.format(cfsname))
            impl.append('template <>\n')
            impl.append(cfsname + ' _arg<' + cfsname + '>(std::vector<ivy_value> &args, unsigned idx, int bound);\n')
            impl.append('template <>\n')
            impl.append('void  __ser<' + cfsname + '>(std::vector<char> &res, const ' + cfsname + '&);\n')
            impl.append('template <>\n')
            impl.append('void  __deser<' + cfsname + '>(const std::vector<char> &inp, unsigned &pos, ' + cfsname + ' &res);\n')

    if target.get() in ["test","gen"]:
        for sort_name in sorted(im.module.sort_destructors):
            csname = varname(sort_name)
            cfsname = classname + '::' + csname
            impl.append('template <>\n')
            impl.append('void __from_solver<' + cfsname + '>( gen &g, const  z3::expr &v, ' + cfsname + ' &res);\n')
            impl.append('template <>\n')
            impl.append('z3::expr __to_solver<' + cfsname + '>( gen &g, const  z3::expr &v, ' + cfsname + ' &val);\n')
            impl.append('template <>\n')
            impl.append('void __randomize<' + cfsname + '>( gen &g, const  z3::expr &v);\n')

    for dom in all_ctuples():
        emit_ctuple_equality(impl,dom,classname)

    once_memo = set()
    for native in im.module.natives:
        tag = native_type(native)
        if tag == "impl":
            global native_classname
            native_classname = classname
            code = native_to_str(native)
            native_classname = None
            if code not in once_memo:
                once_memo.add(code)
                impl.append(code)


    impl.append("int " + classname)
    if target.get() in ["gen","test"]:
        impl.append(
"""::___ivy_choose(int rng,const char *name,int id) {
        std::ostringstream ss;
        ss << name << ':' << id;;
        for (unsigned i = 0; i < ___ivy_stack.size(); i++)
            ss << ':' << ___ivy_stack[i];
        return ___ivy_gen->choose(rng,ss.str().c_str());
    }
""")
    else:
        impl.append(
"""::___ivy_choose(int rng,const char *name,int id) {
        return 0;
    }
""")

    emit_cpp_sorts(header)
    declare_all_ctuples(header)
    declare_all_ctuples_hash(header,classname)
    for sym in all_state_symbols():
        if sym_is_member(sym):
            declare_symbol(header,sym)
    for sym in il.sig.constructors:
        declare_symbol(header,sym)
    for sname in il.sig.interp:
        header.append('    int __CARD__' + varname(sname) + ';\n')
    for ldf in im.module.definitions:
        with ivy_ast.ASTContext(ldf):
            emit_derived(header,impl,ldf.formula,classname)

    for native in im.module.natives:
        tag = native_type(native)
        if tag not in ["member","init","header","impl"]:
            raise iu.IvyError(native,"syntax error at token {}".format(tag))
        if tag == "member":
            emit_native(header,impl,native,classname)

    # declare one counter for each progress obligation
    # TRICKY: these symbols are boolean but we create a C++ int
    for df in im.module.progress:
        declare_symbol(header,df.args[0].rep,c_type = 'int')

    header.append('    ');
    emit_param_decls(header,classname,im.module.params)
    header.append(';\n');
    im.module.actions['.init'] = init_method()
    for a in im.module.actions:
        emit_action(header,impl,a,classname)
    emit_tick(header,impl,classname)
    header.append('};\n')

    impl.append(classname + '::')
    emit_param_decls(impl,classname,im.module.params)
    impl.append('{\n')
    enums = set(sym.sort.name for sym in il.sig.constructors)  
    for sortname in enums:
        for i,n in enumerate(il.sig.sorts[sortname].extension):
            impl.append('    {} = {};\n'.format(varname(n),i))
    for sortname in il.sig.interp:
        if sortname in il.sig.sorts:
            impl.append('    __CARD__{} = {};\n'.format(varname(sortname),csortcard(il.sig.sorts[sortname])))
    if target.get() not in ["gen","test"]:
        emit_one_initial_state(impl)
    for native in im.module.natives:
        tag = native_type(native)
        if tag == "init":
            vs = [il.Symbol(v.rep,im.module.sig.sorts[v.sort]) for v in native.args[0].args]
            global indent_level
            indent_level += 1
            open_loop(impl,vs)
            code = native_to_str(native,reference=True)
            indent_code(impl,code)
            close_loop(impl,vs)
            indent_level -= 1

    impl.append('}\n')

    if target.get() in ["gen","test"]:
        sf = header if target.get() == "gen" else impl
        if target.get() == "gen":
            emit_boilerplate1(sf,impl,classname)
        emit_init_gen(sf,impl,classname)
        for name,action in im.module.actions.iteritems():
            if name in im.module.public_actions:
                emit_action_gen(sf,impl,name,action,classname)

    if True or target.get() == "repl":

        for sort_name in sorted(im.module.sort_destructors):
            destrs = im.module.sort_destructors[sort_name]
            sort = im.module.sig.sorts[sort_name]
            csname = varname(sort_name)
            cfsname = classname + '::' + csname
            open_scope(impl,line='std::ostream &operator <<(std::ostream &s, const {} &t)'.format(cfsname))
            code_line(impl,'s<<"{"')
            for idx,sym in enumerate(destrs):
                if idx > 0:
                    code_line(impl,'s<<","')
                code_line(impl,'s<< "' + memname(sym) + ':"')
                dom = sym.sort.dom[1:]
                vs = variables(dom)
                for d,v in zip(dom,vs):
                    code_line(impl,'s << "["')
                    open_loop(impl,[v])
                    code_line(impl,'if ({}) s << ","'.format(varname(v)))
                code_line(impl,'s << t.' + memname(sym) + subscripts(vs))
                for d,v in zip(dom,vs):
                    close_loop(impl,[v])
                    code_line(impl,'s << "]"')
            code_line(impl,'s<<"}"')
            code_line(impl,'return s')
            close_scope(impl)

            open_scope(header,line='bool operator ==(const {} &s, const {} &t)'.format(cfsname,cfsname))
            s = il.Symbol('s',sort)
            t = il.Symbol('t',sort)
            code_line(header,'return ' + code_eval(header,il.And(*[field_eq(s,t,sym) for sym in destrs])))
            close_scope(header)

            impl.append('template <>\n')
            open_scope(impl,line='void  __ser<' + cfsname + '>(std::vector<char> &res, const ' + cfsname + '&t)')
            for idx,sym in enumerate(destrs):
                dom = sym.sort.dom[1:]
                vs = variables(dom)
                for d,v in zip(dom,vs):
                    open_loop(impl,[v])
                code_line(impl,'__ser<' + ctype(sym.sort.rng,classname=classname) + '>(res,t.' + memname(sym) + subscripts(vs) + ')')
                for d,v in zip(dom,vs):
                    close_loop(impl,[v])
            close_scope(impl)


        if target.get() in ["repl","test"]:
            emit_repl_imports(header,impl,classname)
            emit_repl_boilerplate1(header,impl,classname)

            for sort_name in sorted(im.module.sort_destructors):
                destrs = im.module.sort_destructors[sort_name]
                sort = im.module.sig.sorts[sort_name]
                csname = varname(sort_name)
                cfsname = classname + '::' + csname
                impl.append('template <>\n')
                open_scope(impl,line=cfsname + ' _arg<' + cfsname + '>(std::vector<ivy_value> &args, unsigned idx, int bound)')
                code_line(impl,cfsname + ' res')
                code_line(impl,'ivy_value &arg = args[idx]')
                code_line(impl,'if (arg.atom.size() || arg.fields.size() != {}) throw out_of_bounds(idx)'.format(len(destrs)))
                code_line(impl,'std::vector<ivy_value> tmp_args(1)')
                for idx,sym in enumerate(destrs):
                    open_scope(impl,line='if (arg.fields[{}].is_member())'.format(idx))
                    code_line(impl,'tmp_args[0] = arg.fields[{}].fields[0]'.format(idx))
                    fname = memname(sym)
                    code_line(impl,'if (arg.fields[{}].atom != "{}") throw out_of_bounds(idx)'.format(idx,fname))
                    close_scope(impl)
                    open_scope(impl,line='else')
                    code_line(impl,'tmp_args[0] = arg.fields[{}]'.format(idx))
                    close_scope(impl)
                    vs = variables(sym.sort.dom[1:])
                    for v in vs:
                        open_scope(impl)
                        code_line(impl,'ivy_value tmp = tmp_args[0]')
                        code_line(impl,'if(tmp.atom.size() || tmp.fields.size() != {}) throw out_of_bounds(idx)'.format(csortcard(v.sort)))
                        open_loop(impl,[v])
                        code_line(impl,'std::vector<ivy_value> tmp_args(1)')
                        code_line(impl,'tmp_args[0] = tmp.fields[{}]'.format(varname(v)))
                    code_line(impl,'res.'+fname+''.join('[{}]'.format(varname(v)) for v in vs) + ' = _arg<'+ctype(sym.sort.rng,classname=classname)
                              +'>(tmp_args,0,{});\n'.format(csortcard(sym.sort.rng)))
                    for v in vs:
                        close_loop(impl,[v])
                        close_scope(impl)
                code_line(impl,'return res')
                close_scope(impl)

                impl.append('template <>\n')
                open_scope(impl,line='void __deser<' + cfsname + '>(const std::vector<char> &inp, unsigned &pos, ' + cfsname + ' &res)')
                for idx,sym in enumerate(destrs):
                    fname = memname(sym)
                    vs = variables(sym.sort.dom[1:])
                    for v in vs:
                        open_loop(impl,[v])
                    code_line(impl,'__deser(inp,pos,res.'+fname+''.join('[{}]'.format(varname(v)) for v in vs) + ')')
                    for v in vs:
                        close_loop(impl,[v])
                close_scope(impl)
                if target.get() in ["gen","test"]:
                    impl.append('template <>\n')
                    open_scope(impl,line='void  __from_solver<' + cfsname + '>( gen &g, const  z3::expr &v,' + cfsname + ' &res)')
                    for idx,sym in enumerate(destrs):
                        fname = memname(sym)
                        vs = variables(sym.sort.dom[1:])
                        for v in vs:
                            open_loop(impl,[v])
                        code_line(impl,'__from_solver(g,g.apply("'+sym.name+'",v'+ ''.join(',g.int_to_z3(g.sort("'+v.sort.name+'"),'+varname(v)+')' for v in vs)+'),res.'+fname+''.join('[{}]'.format(varname(v)) for v in vs) + ')')
                        for v in vs:
                            close_loop(impl,[v])
                    close_scope(impl)
                    impl.append('template <>\n')
                    open_scope(impl,line='z3::expr  __to_solver<' + cfsname + '>( gen &g, const  z3::expr &v,' + cfsname + ' &val)')
                    code_line(impl,'z3::expr res = g.ctx.bool_val(1)')
                    for idx,sym in enumerate(destrs):
                        fname = memname(sym)
                        vs = variables(sym.sort.dom[1:])
                        for v in vs:
                            open_loop(impl,[v])
                        code_line(impl,'res = res && __to_solver(g,g.apply("'+sym.name+'",v'+ ''.join(',g.int_to_z3(g.sort("'+v.sort.name+'"),'+varname(v)+')' for v in vs)+'),val.'+fname+''.join('[{}]'.format(varname(v)) for v in vs) + ')')
                        for v in vs:
                            close_loop(impl,[v])
                    code_line(impl,'return res')
                    close_scope(impl)
                    impl.append('template <>\n')
                    open_scope(impl,line='void  __randomize<' + cfsname + '>( gen &g, const  z3::expr &v)')
                    for idx,sym in enumerate(destrs):
                        fname = memname(sym)
                        vs = variables(sym.sort.dom[1:])
                        for v in vs:
                            open_loop(impl,[v])
                        code_line(impl,'__randomize<'+ctypefull(sym.sort.rng,classname=classname)+'>(g,g.apply("'+sym.name+'",v'+ ''.join(',g.int_to_z3(g.sort("'+v.sort.name+'"),'+varname(v)+')' for v in vs)+'))')
                        for v in vs:
                            close_loop(impl,[v])
                    close_scope(impl)

            emit_all_ctuples_to_solver(impl,classname)


            emit_repl_boilerplate1a(header,impl,classname)
            for actname in sorted(im.module.public_actions):
                username = actname[4:] if actname.startswith("ext:") else actname
                action = im.module.actions[actname]
                getargs = ','.join('_arg<{}>(args,{},{})'.format(ctype(x.sort,classname=classname),idx,csortcard(x.sort)) for idx,x in enumerate(action.formal_params))
                thing = "ivy.methodname(getargs)"
                if action.formal_returns:
                    thing = 'std::cout << "= " << ' + thing + " << std::endl"
                impl.append("""
                if (action == "actname") {
                    check_arity(args,numargs,action);
                    thing;
                }
                else
    """.replace('thing',thing).replace('actname',username).replace('methodname',varname(actname)).replace('numargs',str(len(action.formal_params))).replace('getargs',getargs))
            emit_repl_boilerplate2(header,impl,classname)


            impl.append("int main(int argc, char **argv){\n")
            impl.append("    if (argc != "+str(len(im.module.params)+1)+"){\n")
            impl.append('        std::cerr << "usage: {} {}\\n";\n'
                        .format(classname,' '.join(map(varname,im.module.params))))
            impl.append('        exit(1);\n    }\n')
            impl.append('    std::vector<std::string> args;\n')
            impl.append('    std::vector<ivy_value> arg_values(1);\n')
            impl.append('    for(int i = 1; i < argc;i++){args.push_back(argv[i]);}\n')
            for idx,s in enumerate(im.module.params):
                impl.append('    int p__'+varname(s)+';\n')
                impl.append('    try {\n')
                impl.append('        int pos = 0;\n')
                impl.append('        arg_values[{}] = parse_value(args[{}],pos);\n'.format(idx,idx))
                impl.append('        p__'+varname(s)+' =  _arg<{}>(arg_values,{},{});\n'
                            .format(ctype(s.sort,classname=classname),idx,csortcard(s.sort)))
                impl.append('    }\n    catch(out_of_bounds &) {\n')
                impl.append('        std::cerr << "parameter {} out of bounds\\n";\n'.format(varname(s)))
                impl.append('        exit(1);\n    }\n')
                impl.append('    catch(syntax_error &) {\n')
                impl.append('        std::cerr << "syntax error in command argument\\n";\n')
                impl.append('        exit(1);\n    }\n')
            cp = '(' + ','.join('p__'+varname(s) for s in im.module.params) + ')' if im.module.params else ''
            impl.append('    {}_repl ivy{};\n'
                        .format(classname,cp))
            if target.get() == "test":
                emit_repl_boilerplate3test(header,impl,classname)
            else:
                emit_repl_boilerplate3(header,impl,classname)


        
    return ''.join(header) , ''.join(impl)


def check_representable(sym,ast=None,skip_args=0):
    return True
    sort = sym.sort
    if hasattr(sort,'dom'):
        for domsort in sort.dom[skip_args:]:
            card = sort_card(domsort)
            if card == None:
                raise iu.IvyError(ast,'cannot compile "{}" because type {} is uninterpreted'.format(sym,domsort))
            if card > 16:
                raise iu.IvyError(ast,'cannot compile "{}" because type {} is large'.format(sym,domsort))

def cstr(term):
    if isinstance(term,il.Symbol):
        return varname(term).split('!')[-1]
    return il.fmla_to_str_ambiguous(term)

def subscripts(vs):
    return ''.join('['+varname(v)+']' for v in vs)

def variables(sorts,start=0):
    return [il.Variable('X__'+str(idx+start),s) for idx,s in enumerate(sorts)]


def assign_symbol_value(header,lhs_text,m,v,same=False):
    sort = v.sort
    if hasattr(sort,'name') and sort.name in im.module.sort_destructors:
        for sym in im.module.sort_destructors[sort.name]:
            check_representable(sym,skip_args=1)
            dom = sym.sort.dom[1:]
            if dom:
                if same:
                    vs = variables(dom)
                    open_loop(header,vs)
                    term = sym(*([v] + vs))
                    ctext = memname(sym) + ''.join('['+varname(a)+']' for a in vs)
                    assign_symbol_value(header,lhs_text+[ctext],m,term,same)
                    close_loop(header,vs)
                else:
                    for args in itertools.product(*[range(sort_card(s)) for s in dom]):
                        term = sym(*([v] + [il.Symbol(str(a),s) for a,s in zip(args,dom)]))
                        ctext = memname(sym) + ''.join('['+str(a)+']' for a in args)
                        assign_symbol_value(header,lhs_text+[ctext],m,term,same)
            else:
                assign_symbol_value(header,lhs_text+[memname(sym)],m,sym(v),same)
    else:
        mv = m(v)
        if mv != None:           
            header.append('    ' + '.'.join(lhs_text) + ' = ' + m(v) + ';\n')
        

def assign_symbol_from_model(header,sym,m):
    if slv.solver_name(sym) == None:
        return # skip interpreted symbols
    if sym.name in im.module.destructor_sorts:
        return # skip structs
    name, sort = sym.name,sym.sort
    check_representable(sym)
    fun = lambda v: cstr(m.eval_to_constant(v))
    if hasattr(sort,'dom'):
        for args in itertools.product(*[range(sort_card(s)) for s in sym.sort.dom]):
            term = sym(*[il.Symbol(str(a),s) for a,s in zip(args,sym.sort.dom)])
            ctext = varname(sym.name) + ''.join('['+str(a)+']' for a in args)
            assign_symbol_value(header,[ctext],fun,term)
    else:
        assign_symbol_value(header,[varname(sym.name)],fun,sym)

def assign_array_from_model(impl,sym,prefix,fun):
    name, sort = sym.name,sym.sort
    if hasattr(sort,'dom'):
        vs = variables(sym.sort.dom)
        for v in vs:
            open_loop(impl,[v])
        term = sym(*vs)
        ctext = prefix + varname(sym.name) + ''.join('['+v.name+']' for v in vs)
        assign_symbol_value(impl,[ctext],fun,term)
        for v in vs:
            close_loop(impl,[v])
    else:
        assign_symbol_value(impl,[prefix+varname(sym.name)],fun,sym)
        
def check_init_cond(kind,lfmlas):
    params = set(im.module.params)
    for lfmla in lfmlas:
        if any(c in params for c in ilu.used_symbols_ast(lfmla.formula)):
            raise iu.IvyError(lfmla,"{} depends on stripped parameter".format(kind))
        
    
def emit_one_initial_state(header):
    check_init_cond("initial condition",im.module.labeled_inits)
    check_init_cond("axiom",im.module.labeled_axioms)
        
    clauses = ilu.and_clauses(im.module.init_cond,im.module.background_theory())
    m = slv.get_model_clauses(clauses)
    if m == None:
        raise IvyError(None,'Initial condition is inconsistent')
    used = ilu.used_symbols_clauses(clauses)
    for sym in all_state_symbols():
        if sym in im.module.params:
            name = varname(sym)
            header.append('    this->{} = {};\n'.format(name,name))
        elif sym not in is_derived and not is_native_sym(sym):
            if sym in used:
                assign_symbol_from_model(header,sym,m)
            else:
                mk_nondet_sym(header,sym,'init',0)
    action = ia.Sequence(*[a for n,a in im.module.initializers])
    action.emit(header)



def emit_constant(self,header,code):
    if isinstance(self,il.Symbol) and self.is_numeral():
        if is_native_sym(self) or self.sort.name in im.module.sort_destructors:
            raise iu.IvyError(None,"cannot compile symbol {} of sort {}".format(self.name,self.sort))
        if self.sort.name in il.sig.interp and il.sig.interp[self.sort.name].startswith('bv['):
            sname,sparms = parse_int_params(il.sig.interp[self.sort.name])
            code.append('(' + varname(self.name) + ' & ' + str((1 << sparms[0]) -1) + ')')
            return
    code.append(varname(self.name))

il.Symbol.emit = emit_constant
il.Variable.emit = emit_constant

def emit_native_expr(self,header,code):
    code.append(native_expr_full(self))

ivy_ast.NativeExpr.emit = emit_native_expr

def parse_int_params(name):
    spl = name.split('[')
    name,things = spl[0],spl[1:]
#    print "things:".format(things)
    if not all(t.endswith(']') for t in things):
        raise SyntaxError()
    return name,[int(t[:-1]) for t in things]

def emit_special_op(self,op,header,code):
    if op == 'concat':
        sort_name = il.sig.interp[self.args[1].sort.name]
        sname,sparms = parse_int_params(sort_name)
        if sname == 'bv' and len(sparms) == 1:
            code.append('(')
            self.args[0].emit(header,code)
            code.append(' << {} | '.format(sparms[0]))
            self.args[1].emit(header,code)
            code.append(')')
            return
    if op.startswith('bfe['):
        opname,opparms = parse_int_params(op)
        mask = (1 << (opparms[0]-opparms[1]+1)) - 1
        code.append('(')
        self.args[0].emit(header,code)
        code.append(' >> {} & {})'.format(opparms[1],mask))
        return
    raise iu.IvyError(self,"operator {} cannot be emitted as C++".format(op))

def emit_bv_op(self,header,code):
    sname,sparms = parse_int_params(il.sig.interp[self.sort.name])
    code.append('(')
    code.append('(')
    self.args[0].emit(header,code)
    code.append(' {} '.format(self.func.name))
    self.args[1].emit(header,code)
    code.append(') & {})'.format((1 << sparms[0])-1))

def is_bv_term(self):
    return (il.is_first_order_sort(self.sort)
            and self.sort.name in il.sig.interp
            and il.sig.interp[self.sort.name].startswith('bv['))

def emit_app(self,header,code):
    # handle macros
    if il.is_macro(self):
        return il.expand_macro(self).emit(header,code)
    # handle interpreted ops
    if slv.solver_name(self.func) == None:
        if self.func.name in il.sig.interp:
            op = il.sig.interp[self.func.name]
            emit_special_op(self,op,header,code)
            return
        assert len(self.args) == 2 # handle only binary ops for now
        if is_bv_term(self):
            emit_bv_op(self,header,code)
            return
        code.append('(')
        self.args[0].emit(header,code)
        code.append(' {} '.format(self.func.name))
        self.args[1].emit(header,code)
        code.append(')')
        return 
    # handle destructors
    skip_params = 0
    if self.func.name in im.module.destructor_sorts:
        self.args[0].emit(header,code)
        code.append('.'+memname(self.func))
        skip_params = 1
    # handle uninterpreted ops
    else:
        code.append(varname(self.func.name))
    global is_derived
    if self.func in is_derived:
        code.append('(')
        first = True
        for a in self.args:
            if not first:
                code.append(',')
            a.emit(header,code)
            first = False
        code.append(')')
    elif is_large_type(self.rep.sort) and len(self.args[skip_params:]) > 1:
        code.append('[' + ctuple(self.rep.sort.dom[skip_params:]) + '(')
        first = True
        for a in self.args[skip_params:]:
            if not first:
                code.append(',')
            a.emit(header,code)
            first = False
        code.append(')]')
    else: 
        for a in self.args[skip_params:]:
            code.append('[')
            a.emit(header,code)
            code.append(']')

lg.Apply.emit = emit_app

class HavocSymbol(object):
    def __init__(self,sort,name,unique_id):
        self.sort,self.name,self.unique_id = sort,name,unique_id
        self.args = []
    def clone(self,args):
        return HavocSymbol(self.sort,self.name,self.unique_id)

def emit_havoc_symbol(self,header,code):
    sym = il.Symbol(new_temp(header,sort=self.sort),self.sort)
    mk_nondet_sym(header,sym,self.name,self.unique_id)
    code.append(sym.name)
    

HavocSymbol.emit = emit_havoc_symbol


temp_ctr = 0

def new_temp(header,sort=None):
    global temp_ctr
    name = '__tmp' + str(temp_ctr)
    temp_ctr += 1
    indent(header)
    header.append(('int' if sort == None else ctype(sort)) + ' ' + name + ';\n')
    return name


def get_bound_exprs(v0,variables,body,exists,res):
    if isinstance(body,il.Not):
        return get_bound_exprs(v0,variables,body.args[0],not exists,res)
    if il.is_app(body) and body.rep.name in ['<','<=','>','>=']:
        res.append((body,not exists))
    if isinstance(body,il.Implies) and not exists:
        get_bound_exprs(v0,variables,body.args[0],not exists,res)
        get_bound_exprs(v0,variables,body.args[1],exists,res)
        return
    if isinstance(body,il.Or) and not exists:
        for arg in body.args:
            get_bound_exprs(v0,variables,arg,exists,res)
        return
    if isinstance(body,il.And) and exists:
        for arg in body.args:
            get_bound_exprs(v0,variables,arg,exists,res)
        return
    
def sort_has_negative_values(sort):
    return sort.name in il.sig.interp and il.sig.interp[sort.name] == 'int'

def get_bounds(header,v0,variables,body,exists):
    bes = []
    get_bound_exprs(v0,variables,body,exists,bes)
    los = []
    his = []
    for be in bes:
        expr,neg = be
        op = expr.rep.name
        strict = op in ['<','>']
        args = expr.args if op in ['<','<='] else [expr.args[1],expr.args[0]]
        if neg:
            strict = not strict
            args = [args[1],args[0]]
        if args[0] == v0 and args[1] != v0 and args[1] not in variables:
            e = code_eval(header,args[1])
            his.append('('+e+')-1' if not strict else e)
        if args[1] == v0 and args[0] != v0 and args[0] not in variables:
            e = code_eval(header,args[0])
            los.append('('+e+')+1' if strict else e)
    if not sort_has_negative_values(v0.sort):
        los.append("0")
    if sort_card(v0.sort) != None:
        his.append(csortcard(v0.sort))
    if not los:
        raise iu.IvyError(None,'cannot find a lower bound for {}'.format(v0))
    if not his:
        raise iu.IvyError(None,'cannot find an upper bound for {}'.format(v0))
    return los[0],his[0]

def emit_quant(variables,body,header,code,exists=False):
    global indent_level
    if len(variables) == 0:
        body.emit(header,code)
        return
    v0 = variables[0]
    variables = variables[1:]
    check_iterable_sort(v0.sort)
    res = new_temp(header)
    idx = v0.name
    indent(header)
    header.append(res + ' = ' + str(0 if exists else 1) + ';\n')
    indent(header)
    lo,hi = get_bounds(header,v0,variables,body,exists)
    header.append('for (int ' + idx + ' = ' + lo + '; ' + idx + ' < ' + hi + '; ' + idx + '++) {\n')
    indent_level += 1
    subcode = []
    emit_quant(variables,body,header,subcode,exists)
    indent(header)
    header.append('if (' + ('!' if not exists else ''))
    header.extend(subcode)
    header.append(') '+ res + ' = ' + str(1 if exists else 0) + ';\n')
    indent_level -= 1
    indent(header)
    header.append('}\n')
    code.append(res)    


lg.ForAll.emit = lambda self,header,code: emit_quant(list(self.variables),self.body,header,code,False)
lg.Exists.emit = lambda self,header,code: emit_quant(list(self.variables),self.body,header,code,True)

def code_line(impl,line):
    indent(impl)
    impl.append(line+';\n')

def code_asgn(impl,lhs,rhs):
    code_line(impl,lhs + ' = ' + rhs)

def code_decl(impl,sort,name):
    code_line(impl,ctype(sort) + ' ' + name)

def code_eval(impl,expr):
    code = []
    expr.emit(impl,code)
    return ''.join(code)

def emit_some(self,header,code):
    if isinstance(self,ivy_ast.Some):
        vs = [il.Variable('X__'+str(idx),p.sort) for idx,p in enumerate(self.params())]
        subst = dict(zip(self.params(),vs))
        fmla = ilu.substitute_constants_ast(self.fmla(),subst)
        params = self.params()
    else:
        vs = self.params()
        params = [new_temp(header)]
        fmla = self.fmla()
    for v in vs:
        check_iterable_sort(v.sort)
    some = new_temp(header)
    code_asgn(header,some,'0')
    if isinstance(self,ivy_ast.SomeMinMax):
        minmax = new_temp(header)
    open_loop(header,vs)
    open_if(header,code_eval(header,fmla))
    if isinstance(self,ivy_ast.SomeMinMax):
        index = new_temp(header)
        idxfmla =  ilu.substitute_constants_ast(self.index(),subst)
        code_asgn(header,index,code_eval(header,idxfmla))
        open_if(header,some)
        sort = self.index().sort
        op = il.Symbol('<',il.RelationSort([sort,sort]))
        idx = il.Symbol(index,sort)
        mm = il.Symbol(minmax,sort)
        pred = op(idx,mm) if isinstance(self,ivy_ast.SomeMin) else op(mm,idx)
        open_if(header,code_eval(header,il.Not(pred)))
        code_line(header,'continue')
        close_scope(header)
        close_scope(header)
        code_asgn(header,minmax,index)
    for p,v in zip(params,vs):
        code_asgn(header,varname(p),varname(v))
    code_line(header,some+'= 1')
    close_scope(header)
    close_loop(header,vs)
    if isinstance(self,ivy_ast.Some):
        code.append(some)
    else:
        code.append(varname(params[0]))

ivy_ast.Some.emit = emit_some

il.Some.emit = emit_some

def emit_unop(self,header,code,op):
    code.append(op)
    self.args[0].emit(header,code)

lg.Not.emit = lambda self,header,code: emit_unop(self,header,code,'!')

def emit_binop(self,header,code,op,ident=None):
    if len(self.args) == 0:
        assert ident != None
        code.append(ident)
        return
    code.append('(')
    self.args[0].emit(header,code)
    for a in self.args[1:]:
        code.append(' ' + op + ' ')
        a.emit(header,code)
    code.append(')')
    
def emit_implies(self,header,code):
    code.append('(')
    code.append('!')
    self.args[0].emit(header,code)
    code.append(' || ')
    self.args[1].emit(header,code)
    code.append(')')
    

lg.Eq.emit = lambda self,header,code: emit_binop(self,header,code,'==')
lg.Iff.emit = lambda self,header,code: emit_binop(self,header,code,'==')
lg.Implies.emit = emit_implies
lg.And.emit = lambda self,header,code: emit_binop(self,header,code,'&&','true')
lg.Or.emit = lambda self,header,code: emit_binop(self,header,code,'||','false')

def emit_ternop(self,header,code):
    code.append('(')
    self.args[0].emit(header,code)
    code.append(' ? ')
    self.args[1].emit(header,code)
    code.append(' : ')
    self.args[2].emit(header,code)
    code.append(')')
    
lg.Ite.emit = emit_ternop

def emit_assign_simple(self,header):
    code = []
    indent(code)
    self.args[0].emit(header,code)
    code.append(' = ')
    self.args[1].emit(header,code)
    code.append(';\n')    
    header.extend(code)

def emit_assign_large(self,header):
    dom = self.args[0].rep.sort.dom
    vs = variables(dom)
    vs = [x if isinstance(x,il.Variable) else y for x,y in zip(self.args[0].args,vs)]
    eqs = [il.Equals(x,y) for x,y in zip(self.args[0].args,vs) if not isinstance(x,il.Variable)]
    expr = il.Ite(il.And(*eqs),self.args[1],self.args[0].rep(*vs)) if eqs else self.args[1]
    global thunks

    code_line(header,varname(self.args[0].rep)+' = ' + make_thunk(thunks,vs,expr))

def emit_assign(self,header):
    global indent_level
    with ivy_ast.ASTContext(self):
        if is_large_type(self.args[0].rep.sort) and lu.free_variables(self.args[0]):
            emit_assign_large(self,header)
            return
        vs = list(lu.free_variables(self.args[0]))
        for v in vs:
            check_iterable_sort(v.sort)
        if len(vs) == 0:
            emit_assign_simple(self,header)
            return
        global temp_ctr
        tmp = '__tmp' + str(temp_ctr)
        temp_ctr += 1
        indent(header)
        header.append(ctype(self.args[1].sort) + '  ' + tmp)
        for v in vs:
            header.append('[' + str(sort_card(v.sort)) + ']')
        header.append(';\n')
        for idx in vs:
            indent(header)
            header.append('for (int ' + idx.name + ' = 0; ' + idx.name + ' < ' + str(sort_card(idx.sort)) + '; ' + idx.name + '++) {\n')
            indent_level += 1
        code = []
        indent(code)
        code.append(tmp + ''.join('['+varname(v.name)+']' for v in vs) + ' = ')
        self.args[1].emit(header,code)
        code.append(';\n')    
        header.extend(code)
        for idx in vs:
            indent_level -= 1
            indent(header)
            header.append('}\n')
        for idx in vs:
            indent(header)
            header.append('for (int ' + idx.name + ' = 0; ' + idx.name + ' < ' + str(sort_card(idx.sort)) + '; ' + idx.name + '++) {\n')
            indent_level += 1
        code = []
        indent(code)
        self.args[0].emit(header,code)
        code.append(' = ' + tmp + ''.join('['+varname(v.name)+']' for v in vs) + ';\n')
        header.extend(code)
        for idx in vs:
            indent_level -= 1
            indent(header)
            header.append('}\n')
    
ia.AssignAction.emit = emit_assign

def emit_havoc(self,header):
    print self
    print self.lineno
    assert False

ia.HavocAction.emit = emit_havoc

def emit_sequence(self,header):
    global indent_level
    indent(header)
    header.append('{\n')
    indent_level += 1
    for a in self.args:
        a.emit(header)
    indent_level -= 1 
    indent(header)
    header.append('}\n')

ia.Sequence.emit = emit_sequence

def emit_assert(self,header):
    code = []
    indent(code)
    code.append('ivy_assert(')
    with ivy_ast.ASTContext(self):
        il.close_formula(self.args[0]).emit(header,code)
    code.append(', "{}");\n'.format(iu.lineno_str(self)))    
    header.extend(code)

ia.AssertAction.emit = emit_assert

def emit_assume(self,header):
    code = []
    indent(code)
    code.append('ivy_assume(')
    il.close_formula(self.args[0]).emit(header,code)
    code.append(', "{}");\n'.format(iu.lineno_str(self)))    
    header.extend(code)

ia.AssumeAction.emit = emit_assume


def emit_call(self,header):
    indent(header)
    header.append('___ivy_stack.push_back(' + str(self.unique_id) + ');\n')
    code = []
    indent(code)
    if len(self.args) == 2:
        self.args[1].emit(header,code)
        code.append(' = ')
    code.append(varname(str(self.args[0].rep)) + '(')
    first = True
    for p in self.args[0].args:
        if not first:
            code.append(', ')
        p.emit(header,code)
        first = False
    code.append(');\n')    
    header.extend(code)
    indent(header)
    header.append('___ivy_stack.pop_back();\n')

ia.CallAction.emit = emit_call

def local_start(header,params,nondet_id=None):
    global indent_level
    indent(header)
    header.append('{\n')
    indent_level += 1
    for p in params:
        indent(header)
        header.append(ctype(p.sort) + ' ' + varname(p.name) + ';\n')
        if nondet_id != None:
            mk_nondet_sym(header,p,p.name,nondet_id)

def local_end(header):
    global indent_level
    indent_level -= 1
    indent(header)
    header.append('}\n')


def emit_local(self,header):
    local_start(header,self.args[0:-1],self.unique_id)
    self.args[-1].emit(header)
    local_end(header)

ia.LocalAction.emit = emit_local

def emit_if(self,header):
    global indent_level
    code = []
    if isinstance(self.args[0],ivy_ast.Some):
        local_start(header,self.args[0].params())
    indent(code)
    code.append('if(');
    self.args[0].emit(header,code)
    header.extend(code)
    header.append('){\n')
    indent_level += 1
    self.args[1].emit(header)
    indent_level -= 1
    indent(header)
    header.append('}\n')
    if len(self.args) == 3:
        indent(header)
        header.append('else {\n')
        indent_level += 1
        self.args[2].emit(header)
        indent_level -= 1
        indent(header)
        header.append('}\n')
    if isinstance(self.args[0],ivy_ast.Some):
        local_end(header)


ia.IfAction.emit = emit_if

def emit_while(self,header):
    global indent_level
    code = []
    open_scope(header,line='while('+code_eval(header,self.args[0])+')')
    self.args[1].emit(header)
    close_scope(header)


ia.WhileAction.emit = emit_while

def emit_choice(self,header):
    global indent_level
    if len(self.args) == 1:
        self.args[0].emit(header)
        return
    tmp = new_temp(header)
    mk_nondet(header,tmp,len(self.args),"___branch",self.unique_id)
    for idx,arg in enumerate(self.args):
        indent(header)
        if idx != 0:
            header.append('else ')
        if idx != len(self.args)-1:
            header.append('if(' + tmp + ' == ' + str(idx) + ')');
        header.append('{\n')
        indent_level += 1
        arg.emit(header)
        indent_level -= 1
        indent(header)
        header.append('}\n')

ia.ChoiceAction.emit = emit_choice

native_classname = None


def native_reference(atom):
    if isinstance(atom,ivy_ast.Atom) and atom.rep in im.module.actions:
        res = thunk_name(atom.rep) + '(this'
        res += ''.join(', ' + varname(arg.rep) for arg in atom.args) + ')'
        return res
    if atom.rep in im.module.sig.sorts:
        return ctype(im.module.sig.sorts[atom.rep],classname=native_classname)
    res = varname(atom.rep)
    for arg in atom.args:
        n = arg.name if hasattr(arg,'name') else arg.rep
        res += '[' + varname(n) + ']'
    return res

def emit_native_action(self,header):
    fields = self.args[0].code.split('`')
    fields = [(native_reference(self.args[int(s)+1]) if idx % 2 == 1 else s) for idx,s in enumerate(fields)]
    indent_code(header,''.join(fields))

ia.NativeAction.emit = emit_native_action

def emit_repl_imports(header,impl,classname):
    pass

def emit_repl_boilerplate1(header,impl,classname):
    impl.append("""

int ask_ret(int bound) {
    int res;
    while(true) {
        std::cout << "? ";
        std::cin >> res;
        if (res >= 0 && res < bound) 
            return res;
        std::cout << "value out of range" << std::endl;
    }
}

""")

    impl.append("""

    class classname_repl : public classname {

    public:

    virtual void ivy_assert(bool truth,const char *msg){
        if (!truth) {
            std::cerr << msg << ": assertion failed\\n";
            exit(1);
        }
    }
    virtual void ivy_assume(bool truth,const char *msg){
        if (!truth) {
            std::cerr << msg << ": assumption failed\\n";
            exit(1);
        }
    }
    """.replace('classname',classname))

    emit_param_decls(impl,classname+'_repl',im.module.params)
    impl.append(' : '+classname+'('+','.join(map(varname,im.module.params))+'){}\n')
    
    for imp in im.module.imports:
        name = imp.imported()
        if not imp.scope() and name in im.module.actions:
            action = im.module.actions[name]
            emit_method_decl(impl,name,action);
            impl.append('{\n    std::cout << "< ' + name[5:] + '"')
            if action.formal_params:
                impl.append(' << "("')
                first = True
                for arg in action.formal_params:
                    if not first:
                        impl.append(' << ","')
                    first = False
                    impl.append(' << {}'.format(varname(arg.rep.name)))
                impl.append(' << ")"')
            impl.append(' << std::endl;\n')
            if action.formal_returns:
                impl.append('    return ask_ret(__CARD__{});\n'.format(action.formal_returns[0].sort))
            impl.append('}\n')

    

    impl.append("""
    };
""")

    impl.append("""
// Override methods to implement low-level network service

bool is_white(int c) {
    return (c == ' ' || c == '\\t' || c == '\\n');
}

bool is_ident(int c) {
    return c == '_' || c == '.' || (c >= 'A' &&  c <= 'Z')
        || (c >= 'a' &&  c <= 'z')
        || (c >= '0' &&  c <= '9');
}

void skip_white(const std::string& str, int &pos){
    while (pos < str.size() && is_white(str[pos]))
        pos++;
}

struct syntax_error {
};

std::string get_ident(const std::string& str, int &pos) {
    std::string res = "";
    while (pos < str.size() && is_ident(str[pos])) {
        res.push_back(str[pos]);
        pos++;
    }
    if (res.size() == 0)
        throw syntax_error();
    return res;
}

ivy_value parse_value(const std::string& cmd, int &pos) {
    ivy_value res;
    skip_white(cmd,pos);
    if (pos < cmd.size() && cmd[pos] == '[') {
        while (true) {
            pos++;
            skip_white(cmd,pos);
            res.fields.push_back(parse_value(cmd,pos));
            skip_white(cmd,pos);
            if (pos < cmd.size() && cmd[pos] == ']')
                break;
            if (!(pos < cmd.size() && cmd[pos] == ','))
                throw syntax_error();
        }
        pos++;
    }
    else if (pos < cmd.size() && cmd[pos] == '{') {
        while (true) {
            ivy_value field;
            pos++;
            skip_white(cmd,pos);
            field.atom = get_ident(cmd,pos);
            skip_white(cmd,pos);
            if (!(pos < cmd.size() && cmd[pos] == ':'))
                 throw syntax_error();
            pos++;
            skip_white(cmd,pos);
            field.fields.push_back(parse_value(cmd,pos));
            res.fields.push_back(field);
            skip_white(cmd,pos);
            if (pos < cmd.size() && cmd[pos] == '}')
                break;
            if (!(pos < cmd.size() && cmd[pos] == ','))
                throw syntax_error();
        }
        pos++;
    }
    else 
        res.atom = get_ident(cmd,pos);
    return res;
}

void parse_command(const std::string &cmd, std::string &action, std::vector<ivy_value> &args) {
    int pos = 0;
    skip_white(cmd,pos);
    action = get_ident(cmd,pos);
    skip_white(cmd,pos);
    if (pos < cmd.size() && cmd[pos] == '(') {
        pos++;
        skip_white(cmd,pos);
        args.push_back(parse_value(cmd,pos));
        while(true) {
            skip_white(cmd,pos);
            if (!(pos < cmd.size() && cmd[pos] == ','))
                break;
            pos++;
            args.push_back(parse_value(cmd,pos));
        }
        if (!(pos < cmd.size() && cmd[pos] == ')'))
            throw syntax_error();
        pos++;
    }
    skip_white(cmd,pos);
    if (pos != cmd.size())
        throw syntax_error();
}

struct bad_arity {
    std::string action;
    int num;
    bad_arity(std::string &_action, unsigned _num) : action(_action), num(_num) {}
};

void check_arity(std::vector<ivy_value> &args, unsigned num, std::string &action) {
    if (args.size() != num)
        throw bad_arity(action,num);
}

template <>
bool _arg<bool>(std::vector<ivy_value> &args, unsigned idx, int bound) {
    if (!(args[idx].atom == "true" || args[idx].atom == "false") || args[idx].fields.size())
        throw out_of_bounds(idx);
    return args[idx].atom == "true";
}

""".replace('classname',classname))


def emit_repl_boilerplate1a(header,impl,classname):
    impl.append("""

class stdin_reader: public reader {
    std::string buf;

    virtual int fdes(){
        return 0;
    }
    virtual void read() {
        char tmp[257];
        int chars = ::read(0,tmp,256);
        tmp[chars] = 0;
        buf += std::string(tmp);
        size_t pos;
        while ((pos = buf.find('\\n')) != std::string::npos) {
            std::string line = buf.substr(0,pos+1);
            buf.erase(0,pos+1);
            process(line);
        }
    }
    virtual void process(const std::string &line) {
        std::cout << line;
    }
};

class cmd_reader: public stdin_reader {

public:
    classname_repl &ivy;    

    cmd_reader(classname_repl &_ivy) : ivy(_ivy) {
        std::cout << "> "; std::cout.flush();
    }

    virtual void process(const std::string &cmd) {
        std::string action;
        std::vector<ivy_value> args;
        try {
            parse_command(cmd,action,args);
""".replace('classname',classname))


def emit_repl_boilerplate2(header,impl,classname):
    impl.append("""
            {
                std::cout << "undefined action: " << action << std::endl;
            }
        }
        catch (syntax_error&) {
            std::cout << "syntax error" << std::endl;
        }
        catch (out_of_bounds &err) {
            std::cout << "argument " << err.idx + 1 << " out of bounds" << std::endl;
        }
        catch (bad_arity &err) {
            std::cout << "action " << err.action << " takes " << err.num  << " input parameters" << std::endl;
        }
        std::cout << "> "; std::cout.flush();
    }
};


std::vector<reader *> readers;

void install_reader(reader *r){
    readers.push_back(r);
}

std::vector<timer *> timers;

void install_timer(timer *r){
    timers.push_back(r);
}
""".replace('classname',classname))

def emit_repl_boilerplate3(header,impl,classname):
    impl.append("""
    install_reader(new cmd_reader(ivy));

    while(true) {

        fd_set rdfds;
        FD_ZERO(&rdfds);
        int maxfds = 0;

        for (unsigned i = 0; i < readers.size(); i++) {
            reader *r = readers[i];
            int fds = r->fdes();
            FD_SET(fds,&rdfds);
            if (fds > maxfds)
                maxfds = fds;
        }

        int timer_min = 1000;
        for (unsigned i = 0; i < timers.size(); i++){
            int t = timers[i]->ms_delay();
            if (t < timer_min) 
                timer_min = t;
        }

        struct timeval timeout;
        timeout.tv_sec = timer_min/1000;
        timeout.tv_usec = 1000 * (timer_min % 1000);

        int foo = select(maxfds+1,&rdfds,0,0,&timeout);

        if (foo < 0)
            {perror("select failed"); exit(1);}
        
        if (foo == 0){
            // std::cout << "TIMEOUT\\n";            
           for (unsigned i = 0; i < timers.size(); i++)
               timers[i]->timeout(timer_min);
        }
        else {
            for (unsigned i = 0; i < readers.size(); i++) {
                reader *r = readers[i];
                if (FD_ISSET(r->fdes(),&rdfds))
                    r->read();
            }
        }            
    }
}
""".replace('classname',classname))

def emit_repl_boilerplate3test(header,impl,classname):
    impl.append("""
        init_gen my_init_gen;
        my_init_gen.generate(ivy);
        std::vector<gen *> generators;
""")
    for actname in sorted(im.module.public_actions):
        action = im.module.actions[actname]
        impl.append("        generators.push_back(new {}_gen);\n".format(varname(actname)))
    impl.append("""

    for(int cycle = 0; cycle < 1000; cycle++) {

        int choices = generators.size() + readers.size() + timers.size();
        int rnd = choices ? (rand() % choices) : 0;
        if (rnd < generators.size()) {
            gen &g = *generators[rnd];
            if (g.generate(ivy))
                g.execute(ivy);
            continue;
        }


        fd_set rdfds;
        FD_ZERO(&rdfds);
        int maxfds = 0;

        for (unsigned i = 0; i < readers.size(); i++) {
            reader *r = readers[i];
            int fds = r->fdes();
            FD_SET(fds,&rdfds);
            if (fds > maxfds)
                maxfds = fds;
        }

        int timer_min = 1;

        struct timeval timeout;
        timeout.tv_sec = timer_min/1000;
        timeout.tv_usec = 1000 * (timer_min % 1000);

        int foo = select(maxfds+1,&rdfds,0,0,&timeout);

        if (foo < 0)
            {perror("select failed"); exit(1);}
        
        if (foo == 0){
            // std::cout << "TIMEOUT\\n";            
           cycle--;
           for (unsigned i = 0; i < timers.size(); i++){
               if (timer_min >= timers[i]->ms_delay()) {
                   cycle++;
                   break;
               }
           }
           for (unsigned i = 0; i < timers.size(); i++)
               timers[i]->timeout(timer_min);
        }
        else {
            for (unsigned i = 0; i < readers.size(); i++) {
                reader *r = readers[i];
                if (FD_ISSET(r->fdes(),&rdfds))
                    r->read();
            }
        }            
    }
}
""".replace('classname',classname))


def emit_boilerplate1(header,impl,classname):
    header.append("""
#include <string>
#include <vector>
#include <sstream>
#include <cstdlib>
#include "z3++.h"
""")
    header.append("""

using namespace hash_space;

class gen : public ivy_gen {

public:
    z3::context ctx;
protected:
    z3::solver slvr;
    z3::model model;

    gen(): slvr(ctx), model(ctx,(Z3_model)0) {}

    hash_map<std::string, z3::sort> enum_sorts;
    hash_map<Z3_sort, z3::func_decl_vector> enum_values;
    hash_map<std::string, z3::func_decl> decls_by_name;
    hash_map<Z3_symbol,int> enum_to_int;
    std::vector<Z3_symbol> sort_names;
    std::vector<Z3_sort> sorts;
    std::vector<Z3_symbol> decl_names;
    std::vector<Z3_func_decl> decls;
    std::vector<z3::expr> alits;


public:
    virtual bool generate(classname& obj)=0;
    virtual bool execute(classname& obj)=0;
    virtual ~gen(){}

    z3::expr mk_apply_expr(const char *decl_name, unsigned num_args, const int *args){
        z3::func_decl decl = decls_by_name.find(decl_name)->second;
        std::vector<z3::expr> expr_args;
        unsigned arity = decl.arity();
        assert(arity == num_args);
        for(unsigned i = 0; i < arity; i ++) {
            z3::sort sort = decl.domain(i);
            expr_args.push_back(int_to_z3(sort,args[i]));
        }
        return decl(arity,&expr_args[0]);
    }

    int eval(const z3::expr &apply_expr) {
        try {
            z3::expr foo = model.eval(apply_expr,true);
            if (foo.is_bv()) {
                assert(foo.is_numeral());
                int v;
                if (Z3_get_numeral_int(ctx,foo,&v) != Z3_TRUE)
                    assert(false && "bit vector value too large for machine int");
                return v;
            }
            assert(foo.is_app());
            if (foo.is_bool())
                return (foo.decl().decl_kind() == Z3_OP_TRUE) ? 1 : 0;
            return enum_to_int[foo.decl().name()];
        }
        catch (const z3::exception &e) {
            std::cout << e << std::endl;
            throw e;
        }
    }

    int eval_apply(const char *decl_name, unsigned num_args, const int *args) {
        z3::expr apply_expr = mk_apply_expr(decl_name,num_args,args);
        //        std::cout << "apply_expr: " << apply_expr << std::endl;
        try {
            z3::expr foo = model.eval(apply_expr,true);
            if (foo.is_bv()) {
                assert(foo.is_numeral());
                int v;
                if (Z3_get_numeral_int(ctx,foo,&v) != Z3_TRUE)
                    assert(false && "bit vector value too large for machine int");
                return v;
            }
            assert(foo.is_app());
            if (foo.is_bool())
                return (foo.decl().decl_kind() == Z3_OP_TRUE) ? 1 : 0;
            return enum_to_int[foo.decl().name()];
        }
        catch (const z3::exception &e) {
            std::cout << e << std::endl;
            throw e;
        }
    }

    int eval_apply(const char *decl_name) {
        return eval_apply(decl_name,0,(int *)0);
    }

    int eval_apply(const char *decl_name, int arg0) {
        return eval_apply(decl_name,1,&arg0);
    }
    
    int eval_apply(const char *decl_name, int arg0, int arg1) {
        int args[2] = {arg0,arg1};
        return eval_apply(decl_name,2,args);
    }

    int eval_apply(const char *decl_name, int arg0, int arg1, int arg2) {
        int args[3] = {arg0,arg1,arg2};
        return eval_apply(decl_name,3,args);
    }

    z3::expr apply(const char *decl_name, std::vector<z3::expr> &expr_args) {
        z3::func_decl decl = decls_by_name.find(decl_name)->second;
        unsigned arity = decl.arity();
        assert(arity == expr_args.size());
        return decl(arity,&expr_args[0]);
    }

    z3::expr apply(const char *decl_name) {
        std::vector<z3::expr> a;
        return apply(decl_name,a);
    }

    z3::expr apply(const char *decl_name, z3::expr arg0) {
        std::vector<z3::expr> a;
        a.push_back(arg0);
        return apply(decl_name,a);
    }
    
    z3::expr apply(const char *decl_name, z3::expr arg0, z3::expr arg1) {
        std::vector<z3::expr> a;
        a.push_back(arg0);
        a.push_back(arg1);
        return apply(decl_name,a);
    }
    
    z3::expr apply(const char *decl_name, z3::expr arg0, z3::expr arg1, z3::expr arg2) {
        std::vector<z3::expr> a;
        a.push_back(arg0);
        a.push_back(arg1);
        a.push_back(arg2);
        return apply(decl_name,a);
    }

    z3::expr int_to_z3(const z3::sort &range, int value) {
        if (range.is_bool())
            return ctx.bool_val(value);
        if (range.is_bv())
            return ctx.bv_val(value,range.bv_size());
        return enum_values.find(range)->second[value]();
    }

    unsigned sort_card(const z3::sort &range) {
        if (range.is_bool())
            return 2;
        if (range.is_bv())
            return 1 << range.bv_size();
        return enum_values.find(range)->second.size();
    }

    int set(const char *decl_name, unsigned num_args, const int *args, int value) {
        z3::func_decl decl = decls_by_name.find(decl_name)->second;
        std::vector<z3::expr> expr_args;
        unsigned arity = decl.arity();
        assert(arity == num_args);
        for(unsigned i = 0; i < arity; i ++) {
            z3::sort sort = decl.domain(i);
            expr_args.push_back(int_to_z3(sort,args[i]));
        }
        z3::expr apply_expr = decl(arity,&expr_args[0]);
        z3::sort range = decl.range();
        z3::expr val_expr = int_to_z3(range,value);
        z3::expr pred = apply_expr == val_expr;
        //        std::cout << "pred: " << pred << std::endl;
        slvr.add(pred);
    }

    int set(const char *decl_name, int value) {
        return set(decl_name,0,(int *)0,value);
    }

    int set(const char *decl_name, int arg0, int value) {
        return set(decl_name,1,&arg0,value);
    }
    
    int set(const char *decl_name, int arg0, int arg1, int value) {
        int args[2] = {arg0,arg1};
        return set(decl_name,2,args,value);
    }

    int set(const char *decl_name, int arg0, int arg1, int arg2, int value) {
        int args[3] = {arg0,arg1,arg2};
        return set(decl_name,3,args,value);
    }

    void randomize(const z3::expr &apply_expr) {
        z3::sort range = apply_expr.get_sort();
        unsigned card = sort_card(range);
        int value = rand() % card;
        z3::expr val_expr = int_to_z3(range,value);
        z3::expr pred = apply_expr == val_expr;
        // std::cout << "pred: " << pred << std::endl;
        std::ostringstream ss;
        ss << "alit:" << alits.size();
        z3::expr alit = ctx.bool_const(ss.str().c_str());
        alits.push_back(alit);
        slvr.add(!alit || pred);
    }

    void randomize(const char *decl_name, unsigned num_args, const int *args) {
        z3::func_decl decl = decls_by_name.find(decl_name)->second;
        z3::expr apply_expr = mk_apply_expr(decl_name,num_args,args);
        z3::sort range = decl.range();
        unsigned card = sort_card(range);
        int value = rand() % card;
        z3::expr val_expr = int_to_z3(range,value);
        z3::expr pred = apply_expr == val_expr;
        // std::cout << "pred: " << pred << std::endl;
        std::ostringstream ss;
        ss << "alit:" << alits.size();
        z3::expr alit = ctx.bool_const(ss.str().c_str());
        alits.push_back(alit);
        slvr.add(!alit || pred);
    }

    void randomize(const char *decl_name) {
        randomize(decl_name,0,(int *)0);
    }

    void randomize(const char *decl_name, int arg0) {
        randomize(decl_name,1,&arg0);
    }
    
    void randomize(const char *decl_name, int arg0, int arg1) {
        int args[2] = {arg0,arg1};
        randomize(decl_name,2,args);
    }

    void randomize(const char *decl_name, int arg0, int arg1, int arg2) {
        int args[3] = {arg0,arg1,arg2};
        randomize(decl_name,3,args);
    }

    void push(){
        slvr.push();
    }

    void pop(){
        slvr.pop();
    }

    z3::sort sort(const char *name) {
        if (std::string("bool") == name)
            return ctx.bool_sort();
        return enum_sorts.find(name)->second;
    }

    void mk_enum(const char *sort_name, unsigned num_values, char const * const * value_names) {
        z3::func_decl_vector cs(ctx), ts(ctx);
        z3::sort sort = ctx.enumeration_sort(sort_name, num_values, value_names, cs, ts);
        // can't use operator[] here because the value classes don't have nullary constructors
        enum_sorts.insert(std::pair<std::string, z3::sort>(sort_name,sort));
        enum_values.insert(std::pair<Z3_sort, z3::func_decl_vector>(sort,cs));
        sort_names.push_back(Z3_mk_string_symbol(ctx,sort_name));
        sorts.push_back(sort);
        for(unsigned i = 0; i < num_values; i++){
            Z3_symbol sym = Z3_mk_string_symbol(ctx,value_names[i]);
            decl_names.push_back(sym);
            decls.push_back(cs[i]);
            enum_to_int[sym] = i;
        }
    }

    void mk_bv(const char *sort_name, unsigned width) {
        z3::sort sort = ctx.bv_sort(width);
        // can't use operator[] here because the value classes don't have nullary constructors
        enum_sorts.insert(std::pair<std::string, z3::sort>(sort_name,sort));
    }

    void mk_sort(const char *sort_name) {
        Z3_symbol symb = Z3_mk_string_symbol(ctx,sort_name);
        z3::sort sort(ctx,Z3_mk_uninterpreted_sort(ctx, symb));
//        z3::sort sort = ctx.uninterpreted_sort(sort_name);
        // can't use operator[] here because the value classes don't have nullary constructors
        enum_sorts.insert(std::pair<std::string, z3::sort>(sort_name,sort));
    }

    void mk_decl(const char *decl_name, unsigned arity, const char **domain_names, const char *range_name) {
        std::vector<z3::sort> domain;
        for (unsigned i = 0; i < arity; i++)
            domain.push_back(enum_sorts.find(domain_names[i])->second);
        std::string bool_name("Bool");
        z3::sort range = (range_name == bool_name) ? ctx.bool_sort() : enum_sorts.find(range_name)->second;   
        z3::func_decl decl = ctx.function(decl_name,arity,&domain[0],range);
        decl_names.push_back(Z3_mk_string_symbol(ctx,decl_name));
        decls.push_back(decl);
        decls_by_name.insert(std::pair<std::string, z3::func_decl>(decl_name,decl));
    }

    void mk_const(const char *const_name, const char *sort_name) {
        mk_decl(const_name,0,0,sort_name);
    }

    void add(const std::string &z3inp) {
        z3::expr fmla(ctx,Z3_parse_smtlib2_string(ctx, z3inp.c_str(), sort_names.size(), &sort_names[0], &sorts[0], decl_names.size(), &decl_names[0], &decls[0]));
        ctx.check_error();

        slvr.add(fmla);
    }

    bool solve() {
        // std::cout << alits.size();
        while(true){
            z3::check_result res = slvr.check(alits.size(),&alits[0]);
            if (res != z3::unsat)
                break;
            z3::expr_vector core = slvr.unsat_core();
            if (core.size() == 0)
                return false;
            unsigned idx = rand() % core.size();
            z3::expr to_delete = core[idx];
            for (unsigned i = 0; i < alits.size(); i++)
                if (z3::eq(alits[i],to_delete)) {
                    alits[i] = alits.back();
                    alits.pop_back();
                    break;
                }
        }
        model = slvr.get_model();
        alits.clear();
        //        std::cout << model;
        return true;
    }

    int choose(int rng, const char *name){
        if (decls_by_name.find(name) == decls_by_name.end())
            return 0;
        return eval_apply(name);
    }
};
""".replace('classname',classname))
    impl.append(hash_cpp)

target = iu.EnumeratedParameter("target",["impl","gen","repl","test"],"gen")
opt_classname = iu.Parameter("classname","")
opt_build = iu.BooleanParameter("build",False)


def main():
    ia.set_determinize(True)
    slv.set_use_native_enums(True)
    iso.set_interpret_all_sorts(True)
    ivy.read_params()
    iu.set_parameters({'coi':'false',"create_imports":'true',"enforce_axioms":'true'})
    if target.get() == "gen":
        iu.set_parameters({'filter_symbols':'false'})
        
    with im.Module():
        ivy.ivy_init()

        basename = opt_classname.get() or im.module.name
        classname = varname(basename)
        with iu.ErrorPrinter():
            header,impl = module_to_cpp_class(classname,basename)
#        print header
#        print impl
        f = open(basename+'.h','w')
        f.write(header)
        f.close()
        f = open(basename+'.cpp','w')
        f.write(impl)
        f.close()
    if opt_build.get():
        cmd = "g++ -I $Z3DIR/include -L $Z3DIR/lib -g -o {} {}.cpp -lz3".format(basename,basename)
        print cmd
        import os
        exit(os.system(cmd))

if __name__ == "__main__":
    main()
        
hash_h = """
/*++
  Copyright (c) Microsoft Corporation

  This hash template is borrowed from Microsoft Z3
  (https://github.com/Z3Prover/z3).

  Simple implementation of bucket-list hash tables conforming roughly
  to SGI hash_map and hash_set interfaces, though not all members are
  implemented.

  These hash tables have the property that insert preserves iterators
  and references to elements.

  This package lives in namespace hash_space. Specializations of
  class "hash" should be made in this namespace.

  --*/

#ifndef HASH_H
#define HASH_H

#ifdef _WINDOWS
#pragma warning(disable:4267)
#endif

#include <string>
#include <vector>
#include <iterator>

namespace hash_space {

    unsigned string_hash(const char * str, unsigned length, unsigned init_value);

    template <typename T> class hash {
    public:
        size_t operator()(const T &s) const {
            return s.__hash();
        }
    };

    template <>
        class hash<int> {
    public:
        size_t operator()(const int &s) const {
            return s;
        }
    };

    template <>
        class hash<bool> {
    public:
        size_t operator()(const bool &s) const {
            return s;
        }
    };

    template <>
        class hash<std::string> {
    public:
        size_t operator()(const std::string &s) const {
            return string_hash(s.c_str(), s.size(), 0);
        }
    };

    template <>
        class hash<std::pair<int,int> > {
    public:
        size_t operator()(const std::pair<int,int> &p) const {
            return p.first + p.second;
        }
    };

    template <typename T>
        class hash<std::vector<T> > {
    public:
        size_t operator()(const std::vector<T> &p) const {
            hash<T> h;
            size_t res = 0;
            for (unsigned i = 0; i < p.size(); i++)
                res += h(p[i]);
            return res;
        }
    };

    template <class T>
        class hash<std::pair<T *, T *> > {
    public:
        size_t operator()(const std::pair<T *,T *> &p) const {
            return (size_t)p.first + (size_t)p.second;
        }
    };

    template <class T>
        class hash<T *> {
    public:
        size_t operator()(T * const &p) const {
            return (size_t)p;
        }
    };

    enum { num_primes = 29 };

    static const unsigned long primes[num_primes] =
        {
            7ul,
            53ul,
            97ul,
            193ul,
            389ul,
            769ul,
            1543ul,
            3079ul,
            6151ul,
            12289ul,
            24593ul,
            49157ul,
            98317ul,
            196613ul,
            393241ul,
            786433ul,
            1572869ul,
            3145739ul,
            6291469ul,
            12582917ul,
            25165843ul,
            50331653ul,
            100663319ul,
            201326611ul,
            402653189ul,
            805306457ul,
            1610612741ul,
            3221225473ul,
            4294967291ul
        };

    inline unsigned long next_prime(unsigned long n) {
        const unsigned long* to = primes + (int)num_primes;
        for(const unsigned long* p = primes; p < to; p++)
            if(*p >= n) return *p;
        return primes[num_primes-1];
    }

    template<class Value, class Key, class HashFun, class GetKey, class KeyEqFun>
        class hashtable
    {
    public:

        typedef Value &reference;
        typedef const Value &const_reference;
    
        struct Entry
        {
            Entry* next;
            Value val;
      
        Entry(const Value &_val) : val(_val) {next = 0;}
        };
    

        struct iterator
        {      
            Entry* ent;
            hashtable* tab;

            typedef std::forward_iterator_tag iterator_category;
            typedef Value value_type;
            typedef std::ptrdiff_t difference_type;
            typedef size_t size_type;
            typedef Value& reference;
            typedef Value* pointer;

        iterator(Entry* _ent, hashtable* _tab) : ent(_ent), tab(_tab) { }

            iterator() { }

            Value &operator*() const { return ent->val; }

            Value *operator->() const { return &(operator*()); }

            iterator &operator++() {
                Entry *old = ent;
                ent = ent->next;
                if (!ent) {
                    size_t bucket = tab->get_bucket(old->val);
                    while (!ent && ++bucket < tab->buckets.size())
                        ent = tab->buckets[bucket];
                }
                return *this;
            }

            iterator operator++(int) {
                iterator tmp = *this;
                operator++();
                return tmp;
            }


            bool operator==(const iterator& it) const { 
                return ent == it.ent;
            }

            bool operator!=(const iterator& it) const {
                return ent != it.ent;
            }
        };

        struct const_iterator
        {      
            const Entry* ent;
            const hashtable* tab;

            typedef std::forward_iterator_tag iterator_category;
            typedef Value value_type;
            typedef std::ptrdiff_t difference_type;
            typedef size_t size_type;
            typedef const Value& reference;
            typedef const Value* pointer;

        const_iterator(const Entry* _ent, const hashtable* _tab) : ent(_ent), tab(_tab) { }

            const_iterator() { }

            const Value &operator*() const { return ent->val; }

            const Value *operator->() const { return &(operator*()); }

            const_iterator &operator++() {
                Entry *old = ent;
                ent = ent->next;
                if (!ent) {
                    size_t bucket = tab->get_bucket(old->val);
                    while (!ent && ++bucket < tab->buckets.size())
                        ent = tab->buckets[bucket];
                }
                return *this;
            }

            const_iterator operator++(int) {
                const_iterator tmp = *this;
                operator++();
                return tmp;
            }


            bool operator==(const const_iterator& it) const { 
                return ent == it.ent;
            }

            bool operator!=(const const_iterator& it) const {
                return ent != it.ent;
            }
        };

    private:

        typedef std::vector<Entry*> Table;

        Table buckets;
        size_t entries;
        HashFun hash_fun ;
        GetKey get_key;
        KeyEqFun key_eq_fun;
    
    public:

    hashtable(size_t init_size) : buckets(init_size,(Entry *)0) {
            entries = 0;
        }
    
        hashtable(const hashtable& other) {
            dup(other);
        }

        hashtable& operator= (const hashtable& other) {
            if (&other != this)
                dup(other);
            return *this;
        }

        ~hashtable() {
            clear();
        }

        size_t size() const { 
            return entries;
        }

        bool empty() const { 
            return size() == 0;
        }

        void swap(hashtable& other) {
            buckets.swap(other.buckets);
            std::swap(entries, other.entries);
        }
    
        iterator begin() {
            for (size_t i = 0; i < buckets.size(); ++i)
                if (buckets[i])
                    return iterator(buckets[i], this);
            return end();
        }
    
        iterator end() { 
            return iterator(0, this);
        }

        const_iterator begin() const {
            for (size_t i = 0; i < buckets.size(); ++i)
                if (buckets[i])
                    return const_iterator(buckets[i], this);
            return end();
        }
    
        const_iterator end() const { 
            return const_iterator(0, this);
        }
    
        size_t get_bucket(const Value& val, size_t n) const {
            return hash_fun(get_key(val)) % n;
        }
    
        size_t get_key_bucket(const Key& key) const {
            return hash_fun(key) % buckets.size();
        }

        size_t get_bucket(const Value& val) const {
            return get_bucket(val,buckets.size());
        }

        Entry *lookup(const Value& val, bool ins = false)
        {
            resize(entries + 1);

            size_t n = get_bucket(val);
            Entry* from = buckets[n];
      
            for (Entry* ent = from; ent; ent = ent->next)
                if (key_eq_fun(get_key(ent->val), get_key(val)))
                    return ent;
      
            if(!ins) return 0;

            Entry* tmp = new Entry(val);
            tmp->next = from;
            buckets[n] = tmp;
            ++entries;
            return tmp;
        }

        Entry *lookup_key(const Key& key) const
        {
            size_t n = get_key_bucket(key);
            Entry* from = buckets[n];
      
            for (Entry* ent = from; ent; ent = ent->next)
                if (key_eq_fun(get_key(ent->val), key))
                    return ent;
      
            return 0;
        }

        const_iterator find(const Key& key) const {
            return const_iterator(lookup_key(key),this);
        }

        iterator find(const Key& key) {
            return iterator(lookup_key(key),this);
        }

        std::pair<iterator,bool> insert(const Value& val){
            size_t old_entries = entries;
            Entry *ent = lookup(val,true);
            return std::pair<iterator,bool>(iterator(ent,this),entries > old_entries);
        }
    
        iterator insert(const iterator &it, const Value& val){
            Entry *ent = lookup(val,true);
            return iterator(ent,this);
        }

        size_t erase(const Key& key)
        {
            Entry** p = &(buckets[get_key_bucket(key)]);
            size_t count = 0;
            while(*p){
                Entry *q = *p;
                if (key_eq_fun(get_key(q->val), key)) {
                    ++count;
                    *p = q->next;
                    delete q;
                }
                else
                    p = &(q->next);
            }
            entries -= count;
            return count;
        }

        void resize(size_t new_size) {
            const size_t old_n = buckets.size();
            if (new_size <= old_n) return;
            const size_t n = next_prime(new_size);
            if (n <= old_n) return;
            Table tmp(n, (Entry*)(0));
            for (size_t i = 0; i < old_n; ++i) {
                Entry* ent = buckets[i];
                while (ent) {
                    size_t new_bucket = get_bucket(ent->val, n);
                    buckets[i] = ent->next;
                    ent->next = tmp[new_bucket];
                    tmp[new_bucket] = ent;
                    ent = buckets[i];
                }
            }
            buckets.swap(tmp);
        }
    
        void clear()
        {
            for (size_t i = 0; i < buckets.size(); ++i) {
                for (Entry* ent = buckets[i]; ent != 0;) {
                    Entry* next = ent->next;
                    delete ent;
                    ent = next;
                }
                buckets[i] = 0;
            }
            entries = 0;
        }

        void dup(const hashtable& other)
        {
            clear();
            buckets.resize(other.buckets.size());
            for (size_t i = 0; i < other.buckets.size(); ++i) {
                Entry** to = &buckets[i];
                for (Entry* from = other.buckets[i]; from; from = from->next)
                    to = &((*to = new Entry(from->val))->next);
            }
            entries = other.entries;
        }
    };

    template <typename T> 
        class equal {
    public:
        bool operator()(const T& x, const T &y) const {
            return x == y;
        }
    };

    template <typename T>
        class identity {
    public:
        const T &operator()(const T &x) const {
            return x;
        }
    };

    template <typename T, typename U>
        class proj1 {
    public:
        const T &operator()(const std::pair<T,U> &x) const {
            return x.first;
        }
    };

    template <typename Element, class HashFun = hash<Element>, 
        class EqFun = equal<Element> >
        class hash_set
        : public hashtable<Element,Element,HashFun,identity<Element>,EqFun> {

    public:

    typedef Element value_type;

    hash_set()
    : hashtable<Element,Element,HashFun,identity<Element>,EqFun>(7) {}
    };

    template <typename Key, typename Value, class HashFun = hash<Key>, 
        class EqFun = equal<Key> >
        class hash_map
        : public hashtable<std::pair<Key,Value>,Key,HashFun,proj1<Key,Value>,EqFun> {

    public:

    hash_map()
    : hashtable<std::pair<Key,Value>,Key,HashFun,proj1<Key,Value>,EqFun>(7) {}

    Value &operator[](const Key& key) {
	std::pair<Key,Value> kvp(key,Value());
	return 
	hashtable<std::pair<Key,Value>,Key,HashFun,proj1<Key,Value>,EqFun>::
        lookup(kvp,true)->val.second;
    }
    };

}
#endif
"""

hash_cpp = """
/*++
Copyright (c) Microsoft Corporation

This string hash function is borrowed from Microsoft Z3
(https://github.com/Z3Prover/z3). 

--*/


#define mix(a,b,c)              \\
{                               \\
  a -= b; a -= c; a ^= (c>>13); \\
  b -= c; b -= a; b ^= (a<<8);  \\
  c -= a; c -= b; c ^= (b>>13); \\
  a -= b; a -= c; a ^= (c>>12); \\
  b -= c; b -= a; b ^= (a<<16); \\
  c -= a; c -= b; c ^= (b>>5);  \\
  a -= b; a -= c; a ^= (c>>3);  \\
  b -= c; b -= a; b ^= (a<<10); \\
  c -= a; c -= b; c ^= (b>>15); \\
}

#define __fallthrough

namespace hash_space {

// I'm using Bob Jenkin's hash function.
// http://burtleburtle.net/bob/hash/doobs.html
unsigned string_hash(const char * str, unsigned length, unsigned init_value) {
    register unsigned a, b, c, len;

    /* Set up the internal state */
    len = length;
    a = b = 0x9e3779b9;  /* the golden ratio; an arbitrary value */
    c = init_value;      /* the previous hash value */

    /*---------------------------------------- handle most of the key */
    while (len >= 12) {
        a += reinterpret_cast<const unsigned *>(str)[0];
        b += reinterpret_cast<const unsigned *>(str)[1];
        c += reinterpret_cast<const unsigned *>(str)[2];
        mix(a,b,c);
        str += 12; len -= 12;
    }

    /*------------------------------------- handle the last 11 bytes */
    c += length;
    switch(len) {        /* all the case statements fall through */
    case 11: 
        c+=((unsigned)str[10]<<24);
        __fallthrough;
    case 10: 
        c+=((unsigned)str[9]<<16);
        __fallthrough;
    case 9 : 
        c+=((unsigned)str[8]<<8);
        __fallthrough;
        /* the first byte of c is reserved for the length */
    case 8 : 
        b+=((unsigned)str[7]<<24);
        __fallthrough;
    case 7 : 
        b+=((unsigned)str[6]<<16);
        __fallthrough;
    case 6 : 
        b+=((unsigned)str[5]<<8);
        __fallthrough;
    case 5 : 
        b+=str[4];
        __fallthrough;
    case 4 : 
        a+=((unsigned)str[3]<<24);
        __fallthrough;
    case 3 : 
        a+=((unsigned)str[2]<<16);
        __fallthrough;
    case 2 : 
        a+=((unsigned)str[1]<<8);
        __fallthrough;
    case 1 : 
        a+=str[0];
        __fallthrough;
        /* case 0: nothing left to add */
    }
    mix(a,b,c);
    /*-------------------------------------------- report the result */
    return c;
}

}

"""
