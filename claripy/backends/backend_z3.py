import logging
l = logging.getLogger("claripy.backends.backend_z3")

solve_count = 0
cache_count = 0

import weakref

# import and set up Z3
import os
import z3
if "Z3PATH" in os.environ:
	z3_path = os.environ["Z3PATH"]
elif "VIRTUAL_ENV" in os.environ:
	virtual_env = os.environ["VIRTUAL_ENV"]
	z3_path = virtual_env + "/lib/"
else:
	z3_path = "/opt/python/lib/"
z3.init(z3_path + "libz3.so")

from .solver_backend import SolverBackend

#import threading
#import functools
#z3_lock = threading.RLock()
#def synchronized(f):
#	@functools.wraps(f)
#	def synced(self, *args, **kwargs):
#		if not (self._background_solve or (self._background_solve is None and self._claripy.parallel)):
#			return f(self, *args, **kwargs)
#
#		try:
#			#while not z3_lock.acquire(blocking=False): print "ACQUIRING...",__import__('time').sleep(1)
#			z3_lock.acquire()
#			return f(self, *args, **kwargs)
#		finally:
#			z3_lock.release()
#	return synced

def condom(f):
	def z3_condom(*args, **kwargs):
		'''
		The Z3 condom intersects Z3Exceptions and throws a ClaripyZ3Error instead.
		'''
		try:
			return f(*args, **kwargs)
		except z3.Z3Exception as ze:
			raise ClaripyZ3Error("Z3Exception: %s" % ze)
	return z3_condom

class BackendZ3(SolverBackend):
	_split_on = { 'And', 'Or' }

	def __init__(self):
		SolverBackend.__init__(self)
		self._ast_cache = weakref.WeakValueDictionary()
		self._var_cache = weakref.WeakKeyDictionary()
		self._sym_cache = weakref.WeakKeyDictionary()

		# and the operations
		for o in backend_operations - { 'Reverse' }:
			self._op_raw[o] = getattr(z3, o)
		self._op_raw['Reverse'] = self.reverse
		self._op_raw['Identical'] = self.identical

	@condom
	def _size(self, e, result=None):
		if not isinstance(e, z3.BitVecRef) and not isinstance(e, z3.BitVecNumRef):
			l.debug("Unable to determine length of value of type %s", e.__class__)
			raise BackendError("Unable to determine length of value of type %s" % e.__class__)
		return e.size()

	def _name(self, e, result=None): #pylint:disable=unused-argument
		l.warning("BackendZ3.name() called. This is weird.")
		raise BackendError("name is not implemented yet")

	@staticmethod
	@condom
	def reverse(a):
		if a.size() == 8:
			return a
		elif a.size() % 8 != 0:
			raise ClaripyOperationError("can't reverse non-byte sized bitvectors")
		else:
			return z3.Concat(*[z3.Extract(i+7, i, a) for i in range(0, a.size(), 8)])

	def _identical(self, a, b, result=None):
		return a.eq(b)

	@condom
	def _convert(self, obj, result=None):
		if type(obj) is BVV:
			return z3.BitVecVal(obj.value, obj.bits)
		elif obj is True:
			return z3.BoolVal(True)
		elif obj is False:
			return z3.BoolVal(False)
		elif type(obj) in (int, long, float, str):
			return obj
		elif hasattr(obj, '__module__') and obj.__module__ == 'z3':
			return obj
		else:
			l.debug("BackendZ3 encountered unexpected type %s", type(obj))
			raise BackendError("unexpected type %s encountered in BackendZ3" % type(obj))

	@condom
	def call(self, *args, **kwargs):
		return SolverBackend.call(self, *args, **kwargs)

	@condom
	def abstract(self, z):
		#return self._abstract(z, split_on=split_on)[0]
		return self._abstract(z.ctx.ctx, z.ast)

	def _abstract(self, ctx, ast, split_on=None):
		h = z3.Z3_get_ast_hash(ctx, ast)
		if h in self._ast_cache:
			#print "ABSTRACTION CACHED"
			a = self._ast_cache[h]
			return a

		decl = z3.Z3_get_app_decl(ctx, ast)
		decl_num = z3.Z3_get_decl_kind(ctx, decl)
		z3_sort = z3.Z3_get_sort(ctx, ast)

		if decl_num not in z3_op_nums:
			raise ClaripyError("unknown decl kind %d" % decl_num)
		if z3_op_nums[decl_num] not in op_map:
			raise ClaripyError("unknown decl op %s" % z3_op_nums[decl_num])
		op_name = op_map[z3_op_nums[decl_num]]

		split_on = self._split_on if split_on is None else split_on
		new_split_on = split_on if op_name in split_on else set()
		children = [ self._abstract(ctx, z3.Z3_get_app_arg(ctx, ast, i), new_split_on) for i in range(z3.Z3_get_app_num_args(ctx, ast)) ]

		if op_name == 'True':
			return I(self._claripy, True)
		elif op_name == 'False':
			return I(self._claripy, False)
		elif op_name == 'BitVecVal':
			bv_num = long(z3.Z3_get_numeral_string(ctx, ast))
			bv_size = z3.Z3_get_bv_sort_size(ctx, z3_sort)
			return I(self._claripy, BVV(bv_num, bv_size))
		elif op_name == 'UNINTERPRETED': # this *might* be a BitVec ;-)
			bv_name = z3.Z3_get_symbol_string(ctx, z3.Z3_get_decl_name(ctx, decl))
			bv_size = z3.Z3_get_bv_sort_size(ctx, z3_sort)

			#if bv_name.count('_') < 2:
			#	import ipdb; ipdb.set_trace()
			return A(self._claripy, "BitVec", (bv_name, bv_size), variables={ bv_name }, symbolic=True)
		elif op_name == 'Extract':
			hi = z3.Z3_get_decl_int_parameter(ctx, decl, 0)
			lo = z3.Z3_get_decl_int_parameter(ctx, decl, 1)
			args = [ hi, lo ]
		elif op_name in ('SignExt', 'ZeroExt'):
			num = z3.Z3_get_decl_int_parameter(ctx, decl, 0)
			args = [ num ]
		else:
			args = [ ]

		for a in children:
			args.append(a)

		# fix up many-arg __add__
		if op_name in bin_ops and len(args) > 2:
			many_args = args #pylint:disable=unused-variable
			last = args[-1]
			rest = args[:-1]

			a = A(self._claripy, op_name, rest[:2])
			for b in rest[2:]:
				a = A(self._claripy, op_name, [a,b])
			args = [ a, last ]

		a = A(self._claripy, op_name, tuple(args))
		self._ast_cache[h] = a
		return a

	def solver(self, timeout=None):
		s = z3.Solver()
		if timeout is not None:
			if z3.get_version_string() == '4.4.0':
				s.set('soft_timeout', timeout)
				s.set('solver2_timeout', timeout)
			else:
				s.set('timeout', timeout)
		return s

	def _add(self, s, c):
		s.add(*c)

	@condom
	def _check(self, s, extra_constraints=None): #pylint:disable=R0201
		global solve_count
		solve_count += 1
		if extra_constraints is not None:
			s.push()
			s.add(*extra_constraints)

		l.debug("Doing a check!")
		#print "CHECKING"
		satness = s.check() == z3.sat
		#print "CHECKED"

		if extra_constraints is not None:
			s.pop()
		return satness

	@condom
	def _results(self, s, extra_constraints=None, generic_model=True):
		satness = self._check(s, extra_constraints=extra_constraints)
		model = { }
		z3_model = None

		if satness:
			l.debug("sat!")
			z3_model = s.model()
			if generic_model:
				for m_f in z3_model:
					n = m_f.name()
					m = m_f()
					model[n] = self._claripy.model_object(z3_model.eval(m))
		else:
			l.debug("unsat!")

		return Result(satness, model, backend_model=z3_model)

	@condom
	def _eval(self, s, expr, n, extra_constraints=None, result=None):
		global solve_count, cache_count

		#if n == 1 and model is None:
		#	import ipdb; ipdb.set_trace()

		results = [ ]
		model = result.backend_model
		if extra_constraints is not None or n != 1:
			s.push()
		if extra_constraints is not None:
			s.add(*extra_constraints)
			model = None

		for i in range(n):
			if model is None:
				solve_count += 1
				l.debug("Doing a check!")
				if s.check() == z3.sat:
					model = s.model()
			else:
				cache_count += 1

			if model is None:
				break

			if not type(expr) in { int, float, str, bool, long }:
				v = model.eval(expr, model_completion=True)
			else:
				v = expr

			results.append(v)
			if i + 1 != n:
				s.add(expr != v)
				model = None

		if extra_constraints is not None or n != 1:
			s.pop()

		if len(results) == 0:
			raise UnsatError("constraints are unsat")

		return results

	@condom
	def _min(self, s, expr, extra_constraints=None, result=None): #pylint:disable=W0613
		global solve_count

		lo = 0
		hi = 2**expr.size()-1

		numpop = 0
		if extra_constraints is not None:
			s.push()
			numpop += 1
			s.add(*[self.convert(e) for e in extra_constraints])

		while hi-lo > 1:
			middle = (lo + hi)/2
			#l.debug("h/m/l/d: %d %d %d %d", hi, middle, lo, hi-lo)

			s.push()
			s.add(z3.UGE(expr, lo), z3.ULE(expr, middle))
			numpop += 1

			solve_count += 1
			l.debug("Doing a check!")
			if s.check() == z3.sat:
				l.debug("... still sat")
				hi = middle
			else:
				l.debug("... now unsat")
				lo = middle
				s.pop()
				numpop -= 1

		for _ in range(numpop):
			s.pop()

		#l.debug("final hi/lo: %d, %d", hi, lo)

		if hi == lo: return BVV(lo, expr.size())
		else:
			s.push()
			s.add(expr == lo)
			l.debug("Doing a check!")
			if s.check() == z3.sat:
				s.pop()
				return BVV(lo, expr.size())
			else:
				s.pop()
		return BVV(hi, expr.size())

	@condom
	def _max(self, s, expr, extra_constraints=None, result=None): #pylint:disable=W0613
		global solve_count

		lo = 0
		hi = 2**expr.size()-1

		numpop = 0
		if extra_constraints is not None:
			s.push()
			numpop += 1
			s.add(*[self.convert(e) for e in extra_constraints])

		while hi-lo > 1:
			middle = (lo + hi)/2
			#l.debug("h/m/l/d: %d %d %d %d", hi, middle, lo, hi-lo)

			s.push()
			s.add(z3.UGT(expr, middle), z3.ULE(expr, hi))
			numpop += 1

			solve_count += 1
			l.debug("Doing a check!")
			if s.check() == z3.sat:
				l.debug("... still sat")
				lo = middle
			else:
				l.debug("... now unsat")
				hi = middle
				s.pop()
				numpop -= 1
			#l.debug("	now: %d %d %d %d", hi, middle, lo, hi-lo)

		for _ in range(numpop):
			s.pop()

		if hi == lo: return BVV(lo, expr.size())
		else:
			s.push()
			s.add(expr == hi)
			l.debug("Doing a check!")
			if s.check() == z3.sat:
				s.pop()
				return BVV(hi, expr.size())
			else:
				s.pop()
		return BVV(lo, expr.size())

	def _simplify(self, expr): #pylint:disable=W0613,R0201
		raise Exception("This shouldn't be called. Bug Yan.")

	@condom
	def simplify(self, expr):
		if expr._simplified:
			return expr

		l.debug("SIMPLIFYING EXPRESSION")

		#print "SIMPLIFYING"

		expr_raw = self.convert(expr)

		#l.debug("... before: %s (%s)", expr_raw, expr_raw.__class__.__name__)

		if isinstance(expr_raw, z3.BoolRef):
			tactics = z3.Then(z3.Tactic("simplify"), z3.Tactic("propagate-ineqs"), z3.Tactic("propagate-values"), z3.Tactic("unit-subsume-simplify"))
			s = tactics(expr_raw).as_expr()
			n = s.decl().name()

			if n == 'true':
				s = True
			elif n == 'false':
				s = False
		elif isinstance(expr_raw, z3.BitVecRef):
			s = z3.simplify(expr_raw)
		else:
			s = expr_raw

		for b in self._claripy.model_backends:
			try:
				o = I(self._claripy, b.convert(s))
				break
			except BackendError:
				continue
		else:
			o = self.abstract(s)

		#print "SIMPLIFIED"
		#l.debug("... after: %s (%s)", s, s.__class__.__name__)

		return o

#
# this is for the actual->abstract conversion above
#

# these are Z3 operation names for abstraction
z3_op_names = [ _ for _ in dir(z3) if _.startswith('Z3_OP') ]
z3_op_nums = { getattr(z3, _): _ for _ in z3_op_names }

#pylint:disable=bad-continuation
op_map = {
	# Boolean
	'Z3_OP_TRUE': 'True',
	'Z3_OP_FALSE': 'False',
	'Z3_OP_EQ': '__eq__',
	'Z3_OP_DISTINCT': '__ne__',
	'Z3_OP_ITE': 'If',
	'Z3_OP_AND': 'And',
	'Z3_OP_OR': 'Or',
	'Z3_OP_IFF': '__eq__',
	'Z3_OP_XOR': 'Xor',
	'Z3_OP_NOT': 'Not',
	'Z3_OP_IMPLIES': 'Implies',
	#'Z3_OP_OEQ': None,

	# Arithmetic
	#'Z3_OP_ANUM': None,
	#'Z3_OP_AGNUM': None,
	'Z3_OP_LE': '__le__',
	'Z3_OP_GE': '__ge__',
	'Z3_OP_LT': '__lt__',
	'Z3_OP_GT': '__gt__',
	'Z3_OP_ADD': '__add__',
	'Z3_OP_SUB': '__sub__',
	'Z3_OP_UMINUS': '__neg__',
	'Z3_OP_MUL': '__mul__',
	'Z3_OP_DIV': '__div__',
	'Z3_OP_IDIV': '__div__',
	'Z3_OP_REM': '__mod__', # TODO: is this correct?
	'Z3_OP_MOD': '__mod__',
	#'Z3_OP_TO_REAL': None,
	#'Z3_OP_TO_INT': None,
	#'Z3_OP_IS_INT': None,
	'Z3_OP_POWER': '__pow__',

	# Arrays & Sets
	#'Z3_OP_STORE': None,
	#'Z3_OP_SELECT': None,
	#'Z3_OP_CONST_ARRAY': None,
	#'Z3_OP_ARRAY_MAP': None,
	#'Z3_OP_ARRAY_DEFAULT': None,
	#'Z3_OP_SET_UNION': None,
	#'Z3_OP_SET_INTERSECT': None,
	#'Z3_OP_SET_DIFFERENCE': None,
	#'Z3_OP_SET_COMPLEMENT': None,
	#'Z3_OP_SET_SUBSET': None,
	#'Z3_OP_AS_ARRAY': None,

	# Bit-vectors
	'Z3_OP_BNUM': 'BitVecVal',
	#'Z3_OP_BIT1': None, # MAYBE TODO
	#'Z3_OP_BIT0': None, # MAYBE TODO
	'Z3_OP_BNEG': '__neg__',
	'Z3_OP_BADD': '__add__',
	'Z3_OP_BSUB': '__sub__',
	'Z3_OP_BMUL': '__mul__',

	'Z3_OP_BSDIV': '__div__',
	'Z3_OP_BUDIV': '__div__', # TODO: is this correct?
	'Z3_OP_BSREM': '__mod__', # TODO: is this correct?
	'Z3_OP_BUREM': '__mod__', # TODO: is this correct?
	'Z3_OP_BSMOD': '__mod__', # TODO: is this correct?

	# special functions to record the division by 0 cases
	# these are internal functions
	#'Z3_OP_BSDIV0': None,
	#'Z3_OP_BUDIV0': None,
	#'Z3_OP_BSREM0': None,
	#'Z3_OP_BUREM0': None,
	#'Z3_OP_BSMOD0': None,

	'Z3_OP_ULEQ': 'ULE',
	'Z3_OP_SLEQ': '__le__',
	'Z3_OP_UGEQ': 'UGE',
	'Z3_OP_SGEQ': '__ge__',
	'Z3_OP_ULT': 'ULT',
	'Z3_OP_SLT': '__lt__',
	'Z3_OP_UGT': 'UGT',
	'Z3_OP_SGT': '__gt__',

	'Z3_OP_BAND': '__and__',
	'Z3_OP_BOR': '__or__',
	'Z3_OP_BNOT': '__invert__',
	'Z3_OP_BXOR': '__xor__',
	#'Z3_OP_BNAND': None,
	#'Z3_OP_BNOR': None,
	#'Z3_OP_BXNOR': None,

	'Z3_OP_CONCAT': 'Concat',
	'Z3_OP_SIGN_EXT': 'SignExt',
	'Z3_OP_ZERO_EXT': 'ZeroExt',
	'Z3_OP_EXTRACT': 'Extract',
	'Z3_OP_REPEAT': 'RepeatBitVec',

	#'Z3_OP_BREDOR': None,
	#'Z3_OP_BREDAND': None,
	#'Z3_OP_BCOMP': None,

	'Z3_OP_BSHL': '__lshift__',
	'Z3_OP_BLSHR': 'LShR',
	'Z3_OP_BASHR': '__rshift__',
	#'Z3_OP_ROTATE_LEFT': None,
	#'Z3_OP_ROTATE_RIGHT': None,
	'Z3_OP_EXT_ROTATE_LEFT': 'RotateLeft',
	'Z3_OP_EXT_ROTATE_RIGHT': 'RotateRight',

	#'Z3_OP_INT2BV': None,
	#'Z3_OP_BV2INT': None,
	#'Z3_OP_CARRY': None,
	#'Z3_OP_XOR3': None,

	# Proofs
	#'Z3_OP_PR_UNDEF': None,
	#'Z3_OP_PR_TRUE': None,
	#'Z3_OP_PR_ASSERTED': None,
	#'Z3_OP_PR_GOAL': None,
	#'Z3_OP_PR_MODUS_PONENS': None,
	#'Z3_OP_PR_REFLEXIVITY': None,
	#'Z3_OP_PR_SYMMETRY': None,
	#'Z3_OP_PR_TRANSITIVITY': None,
	#'Z3_OP_PR_TRANSITIVITY_STAR': None,
	#'Z3_OP_PR_MONOTONICITY': None,
	#'Z3_OP_PR_QUANT_INTRO': None,
	#'Z3_OP_PR_DISTRIBUTIVITY': None,
	#'Z3_OP_PR_AND_ELIM': None,
	#'Z3_OP_PR_NOT_OR_ELIM': None,
	#'Z3_OP_PR_REWRITE': None,
	#'Z3_OP_PR_REWRITE_STAR': None,
	#'Z3_OP_PR_PULL_QUANT': None,
	#'Z3_OP_PR_PULL_QUANT_STAR': None,
	#'Z3_OP_PR_PUSH_QUANT': None,
	#'Z3_OP_PR_ELIM_UNUSED_VARS': None,
	#'Z3_OP_PR_DER': None,
	#'Z3_OP_PR_QUANT_INST': None,
	#'Z3_OP_PR_HYPOTHESIS': None,
	#'Z3_OP_PR_LEMMA': None,
	#'Z3_OP_PR_UNIT_RESOLUTION': None,
	#'Z3_OP_PR_IFF_TRUE': None,
	#'Z3_OP_PR_IFF_FALSE': None,
	#'Z3_OP_PR_COMMUTATIVITY': None,
	#'Z3_OP_PR_DEF_AXIOM': None,
	#'Z3_OP_PR_DEF_INTRO': None,
	#'Z3_OP_PR_APPLY_DEF': None,
	#'Z3_OP_PR_IFF_OEQ': None,
	#'Z3_OP_PR_NNF_POS': None,
	#'Z3_OP_PR_NNF_NEG': None,
	#'Z3_OP_PR_NNF_STAR': None,
	#'Z3_OP_PR_CNF_STAR': None,
	#'Z3_OP_PR_SKOLEMIZE': None,
	#'Z3_OP_PR_MODUS_PONENS_OEQ': None,
	#'Z3_OP_PR_TH_LEMMA': None,
	#'Z3_OP_PR_HYPER_RESOLVE': None,

	# Sequences
	#'Z3_OP_RA_STORE': None,
	#'Z3_OP_RA_EMPTY': None,
	#'Z3_OP_RA_IS_EMPTY': None,
	#'Z3_OP_RA_JOIN': None,
	#'Z3_OP_RA_UNION': None,
	#'Z3_OP_RA_WIDEN': None,
	#'Z3_OP_RA_PROJECT': None,
	#'Z3_OP_RA_FILTER': None,
	#'Z3_OP_RA_NEGATION_FILTER': None,
	#'Z3_OP_RA_RENAME': None,
	#'Z3_OP_RA_COMPLEMENT': None,
	#'Z3_OP_RA_SELECT': None,
	#'Z3_OP_RA_CLONE': None,
	#'Z3_OP_FD_LT': None,

	# Auxiliary
	#'Z3_OP_LABEL': None,
	#'Z3_OP_LABEL_LIT': None,

	# Datatypes
	#'Z3_OP_DT_CONSTRUCTOR': None,
	#'Z3_OP_DT_RECOGNISER': None,
	#'Z3_OP_DT_ACCESSOR': None,

	'Z3_OP_UNINTERPRETED': 'UNINTERPRETED'
}

from ..ast import A, I
from ..operations import backend_operations, bin_ops
from ..result import Result
from ..bv import BVV
from ..errors import ClaripyError, BackendError, UnsatError, ClaripyOperationError, ClaripyZ3Error
