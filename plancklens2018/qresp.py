"""

FIXME: spin-0 QE sign conventions (stt, ftt, ...)


"""

from __future__ import absolute_import

import os
import numpy as np
import pickle as pk


from plancklens2018 import sql
from plancklens2018.utils import clhash, hash_check
from plancklens2018 import mpi

try:
    from plancklens2018.wigners import wigners  # fortran shared object
    HASWIGNER = True
except:
    print("wigners.so fortran shared object not found")
    print('try f2py -c -m wigners wigners.f90 from the command line in wigners directory')
    print("Falling back on python2 weave implementation")
    HASWIGNER = False
    from plancklens2018.wigners import gaujac, gauleg

verbose = False


def _joincls(cls_list):
    lmaxp1 = np.min([len(cl) for cl in cls_list])
    return np.prod(np.array([cl[:lmaxp1] for cl in cls_list]), axis=0)

class qeleg:
    def __init__(self, spin_in, spin_out, cl):
        self.spin_in = spin_in
        self.spin_ou = spin_out
        self.cl = cl

    def get_lmax(self):
        return len(self.cl) - 1

class qe:
    def __init__(self, leg_a, leg_b, cL):
        assert leg_a.spin_ou +  leg_b.spin_ou >= 0
        self.leg_a = leg_a
        self.leg_b = leg_b
        self.cL = cL

    def __call__(self, lega_dlm, legb_dlm, nside):
        pass
        # FIXME: finish this
        #m = hp.alm2map_spin(lega_dlm, nside, self.leg_a.spin_ou, self.leg_a.get_lmax())
        #m *= hp.alm2map_spin(legb_dlm, nside, self.leg_b.spin_ou, self.leg_b.get_lmax())

    def get_lmax_a(self):
        return self.leg_a.get_lmax()

    def get_lmax_b(self):
        return self.leg_b.get_lmax()

    def get_lmax_qlm(self):
        return len(self.cL)


class resp_leg:
    """ Response instance of a spin-s field to a spin-r anisotropy source.

    Args:
        s (int): spin of the field which responds to the anisotropy  source.
        r (int): spin of the anisotropy source.
        RL (1d array): response coefficients.

    """
    def __init__(self, s, r, RL):
        assert s >= 0, 'do I want this ?'
        self.s = s
        self.r = r
        self.RL = RL

def get_resp_legs(source, lmax):
    """ Defines the responses terms for an anisotropy source.

    Args:
        source (str): anisotropy source (e.g. 'p', 'f', 's', ...).
        lmax (int): responses are given up to lmax.

    Returns:
        4-tuple (r, rR, -rR, cL):  source spin response *r* (positive or zero),
        the harmonic responses for +r and -r (2 1d-arrays), and the scaling between the G/C modes
        and the potentials of interest (for lensing, \phi_{LM}, \Omega_{LM} = \sqrt{L (L + 1)} G_{LM}, C_{LM}).

    """
    lmax_cL = 2 *  lmax
    if source == 'p': # lensing (gradient and curl): _sX -> _sX -  1/2 alpha_1 \eth _sX - 1/2 \alpha_{-1} \bar \eth _sX
        return {s : (1, -0.5 * get_alpha_lower(s, lmax),
                        -0.5 * get_alpha_raise(s, lmax),
                        np.sqrt(np.arange(lmax_cL + 1) * np.arange(1, lmax_cL + 2, dtype=float))) for s in [0, -2, 2]}
    if source == 'f': # Modulation: _sX -> _sX + f _sX.
        return {s : (0, 0.5 * np.ones(lmax + 1, dtype=float),
                        0.5 * np.ones(lmax + 1, dtype=float),
                        np.ones(lmax_cL + 1, dtype=float)) for s in [0, -2, 2]}
    assert 0, source + ' response legs not implemented'

def get_covresp(source, s1, s2, cls, lmax):
    """Covariance matrix response functions in spin space.

        \delta < s_d(n) _td^*(n')> \equiv
        _r\alpha(n) W^{r, st}_l _{s - r}Y_{lm}(n) _tY^*_{lm}(n') +
        _r\alpha^*(n') W^{r, ts}_l _{s}Y_{lm}(n) _{t-r}Y^*_{lm}(n')

    """
    if source in ['p', 'f']:
        # Lensing or modulation field from the field representation
        s_source, prR, mrR, cL_scal = get_resp_legs(source, lmax)[s1]
        coupl = get_coupling(s1, s2, cls)[:lmax + 1]
        return s_source, prR * coupl, mrR * coupl, cL_scal
    elif source == 'stt':
        # Point source 'S^2': Cov -> Cov + B delta_nn' S^2(n) B^\dagger on the diagonal.
        # From the def. there are actually 4 identical W terms hence a factor 1/4.
        cond = s1 == 0 and s2 == 0
        s_source = 0
        prR = 0.25 * np.ones(lmax + 1, dtype=float) * cond
        mrR = 0.25 * np.ones(lmax + 1, dtype=float) * cond
        cL_scal = np.ones(2 * lmax + 1, dtype=float) * cond
        return s_source, prR, mrR, cL_scal
    else:
        assert 0, 'source ' + source + ' not implemented'


def get_qe_sepTP(qe_key, lmax, cls_weight):
    """ Defines the quadratic estimator weights for quadratic estimator key.

    Args:
        qe_key (str): quadratic estimator key (e.g., ptt, p_p, ... )
        lmax (int): weights are built up to lmax.
        cls_weight (dict): CMB spectra entering the weights

    #FIXME:
        * lmax_A, lmax_B, lmaxout!

    The weights are defined by their action on the inverse-variance filtered $ _{s}\\bar X_{lm}$.
    (It is useful to remember that by convention  $_{0}X_{lm} = - T_{lm}$)

    """
    def _sqrt(cl):
        ret = np.zeros(len(cl), dtype=float)
        ret[np.where(cl > 0)] = np.sqrt(cl[np.where(cl > 0)])
        return ret

    if qe_key[0] == 'p' or qe_key[0] == 'x':
        # Lensing estimate (both gradient and curl)
        if qe_key in ['ptt', 'xtt']:
            cL_out = -np.sqrt(np.arange(2 * lmax + 1) * np.arange(1, 2 * lmax + 2, dtype=float) )

            cltt = cls_weight['tt'][:lmax + 1]
            lega = qeleg(0, 0,  np.ones(lmax + 1, dtype=float))
            legb = qeleg(0, 1,  np.sqrt(np.arange(lmax + 1) * np.arange(1, lmax + 2, dtype=float)) * cltt)

            return [qe(lega, legb, cL_out)]

        elif qe_key in ['p_p', 'x_p']:
            qes = []
            cL_out = -np.sqrt(np.arange(2 * lmax + 1) * np.arange(1, 2 * lmax + 2, dtype=float) )
            clee = cls_weight['ee'][:lmax + 1]
            clbb = cls_weight['bb'][:lmax + 1]
            assert np.all(clbb == 0.), 'not implemented (but easy)'
            # E-part. G = -1/2 _{2}P - 1/2 _{-2}P
            lega = qeleg(2, 2, 0.5 * np.ones(lmax + 1, dtype=float))
            legb = qeleg(2, -1,  0.5 * _sqrt(np.arange(2, lmax + 3) * np.arange(-1, lmax, dtype=float)) * clee)
            qes.append(qe(lega, legb, cL_out))

            lega = qeleg(2, 2,  0.5 *np.ones(lmax + 1, dtype=float))
            legb = qeleg(-2, -1, 0.5 * _sqrt(np.arange(2, lmax + 3) * np.arange(-1, lmax, dtype=float)) * clee)
            qes.append(qe(lega, legb, cL_out))

            lega = qeleg(-2, -2, 0.5 *  np.ones(lmax + 1, dtype=float))
            legb = qeleg(2, 3,0.5 * _sqrt(np.arange(-2, lmax - 1) * np.arange(3, lmax + 4, dtype=float)) * clee)
            qes.append(qe(lega, legb, cL_out))

            lega = qeleg(-2, -2, 0.5 *  np.ones(lmax + 1, dtype=float))
            legb = qeleg(-2, 3, 0.5 * _sqrt(np.arange(-2, lmax - 1) * np.arange(3, lmax + 4, dtype=float)) * clee)
            qes.append(qe(lega, legb, cL_out))

            return qes
        elif qe_key in ['p', 'x']:
            cL_out = -np.sqrt(np.arange(2 * lmax + 1) * np.arange(1, 2 * lmax + 2, dtype=float) )
            clte = cls_weight['te'][:lmax + 1] #: _0X_{lm} convention

            qes = get_qe_sepTP('ptt', lmax, cls_weight) + get_qe_sepTP('p_p', lmax, cls_weight)

            # Here Wiener-filtered T contains c_\ell^{TE} \bar E
            lega = qeleg( 0, 0,  np.ones(lmax + 1, dtype=float))
            legb = qeleg( 2, 1,  -0.5 * np.sqrt(np.arange(lmax + 1) * np.arange(1, lmax + 2, dtype=float)) * clte)
            qes.append(qe(lega, legb, cL_out))
            legb = qeleg(-2, 1,  -0.5 * np.sqrt(np.arange(lmax + 1) * np.arange(1, lmax + 2, dtype=float)) * clte)
            qes.append(qe(lega, legb, cL_out))

            # E-mode contains C_\ell^{te} \bar T
            lega = qeleg(2,  2, 0.5 * np.ones(lmax + 1, dtype=float))
            legb = qeleg(0, -1, -_sqrt(np.arange(2, lmax + 3) * np.arange(-1, lmax, dtype=float)) * clte)
            qes.append(qe(lega, legb, cL_out))


            lega = qeleg(-2, -2, 0.5 * np.ones(lmax + 1, dtype=float))
            legb = qeleg( 0,  3,-_sqrt(np.arange(-2, lmax - 1) * np.arange(3, lmax + 4, dtype=float)) * clte)
            qes.append(qe(lega, legb, cL_out))

            return qes

    elif qe_key[0] == 'f':
        if qe_key == 'ftt':
            lega = qeleg(0, 0, -np.ones(lmax + 1, dtype=float))
            legb = qeleg(0, 0, -cls_weight['tt'][:lmax + 1])
            cL_out = np.ones(2 * lmax + 1, dtype=float)
            return [qe(lega, legb, cL_out)]
        else:
            assert 0

    elif qe_key[0] == 's':
        if qe_key == 'stt':
            lega = qeleg(0, 0, - np.ones(lmax + 1, dtype=float))
            legb = qeleg(0, 0, -0.5 * np.ones(lmax + 1, dtype=float))
            cL_out = np.ones(2 * lmax + 1, dtype=float)
            return [qe(lega, legb, cL_out)]
        else:
            assert 0
    else:
        assert 0

class resp_lib_simple:
    def __init__(self, lib_dir, lmax_qe, cls_weight, cls_cmb, fal, lmax_qlm):
        self.lmax_qe = lmax_qe
        self.lmax_qlm = lmax_qlm
        self.cls_weight = cls_weight
        self.cls_cmb = cls_cmb
        self.fal = fal
        self.lib_dir = lib_dir

        fn_hash = os.path.join(lib_dir, 'resp_hash.pk')
        if mpi.rank == 0:
            if not os.path.exists(lib_dir):
                os.makedirs(lib_dir)
            if not os.path.exists(fn_hash):
                pk.dump(self.hashdict(), open(fn_hash, 'wb'))
        mpi.barrier()
        hash_check(pk.load(open(fn_hash, 'rb')), self.hashdict())
        self.npdb = sql.npdb(os.path.join(lib_dir, 'npdb.db'))

    def hashdict(self):
        ret = {'lmaxqe':self.lmax_qe, 'lmax_qlm':self.lmax_qlm}
        for k in self.cls_weight.keys():
            ret['clsweight ' + k] = clhash(self.cls_weight[k])
        for k in self.cls_cmb.keys():
            ret['clscmb ' + k] = clhash(self.cls_cmb[k])
        for k in self.fal.keys():
            ret['fal' + k] = clhash(self.fal[k])
        return ret

    def get_response(self, k, ksource, recache=False):
        fn = 'qe_' + k[1:] + '_source_%s'%ksource + ('_G' if k[0] != 'x' else '_C')
        if self.npdb.get(fn) is None or recache:
            G, C = get_response_sepTP(k, self.lmax_qe, ksource, self.cls_weight, self.cls_cmb, self.fal,
                                      lmax_out=self.lmax_qlm)
            if recache and self.npdb.get(fn) is not None:
                self.npdb.remove('qe_' + k[1:] + '_source_%s' % ksource + '_G')
                self.npdb.remove('qe_' + k[1:] + '_source_%s' % ksource + '_C')
            self.npdb.add('qe_' + k[1:] + '_source_%s' % ksource + '_G', G)
            self.npdb.add('qe_' + k[1:] + '_source_%s' % ksource + '_C', C)
        return self.npdb.get(fn)

class nhl_lib_simple:
    """Analytical unnormalized-N0 library.

    """
    def __init__(self, lib_dir, lmax_qe, cls_weight, cls_ivfs):
        self.lmax_qe = lmax_qe
        self.cls_weight = cls_weight
        self.cls_ivfs = cls_ivfs
        self.lib_dir = lib_dir
        self.npdb = sql.npdb(os.path.join(lib_dir))
        #FIXME: hashdict

    def get_nhl(self, k1, k2, recache=False):
        #FIXME: GC
        assert k1[0] in ['p', 'x'] and k2[0] in ['p', 'x'], 'FIXME'
        if k1[0] != k2[0]: return np.zeros(2 * self.lmax_qe + 1, dtype=float)
        fn = 'anhl_qe_' + k1[1:] + '_qe_' + k2[1:] + ('_G' if k1[0] != 'x' else '_C')
        if self.npdb.get(fn) is None or recache:
            G, C = get_nhl(k1, k2, self.cls_weight, self.cls_ivfs, self.lmax_qe)
            if recache and self.npdb.get(fn) is not None:
                self.npdb.remove('anhl_qe_' + k1[1:] + '_qe_' + k2[1:] + '_G')
                self.npdb.remove('anhl_qe_' + k1[1:] + '_qe_' + k2[1:] + '_C')
            self.npdb.add('anhl_qe_' + k1[1:] + '_qe_' + k2[1:] + '_G', G)
            self.npdb.add('anhl_qe_' + k1[1:] + '_qe_' + k2[1:] + '_C', C)
        return self.npdb.get(fn)


def get_response_jtTP(qe_key, lmax_qe, source, cls_weight, cls_cmb, fabl_leg1, fabl_leg2=None, lmax_out=None, ret_terms=False):
    """QE. response assuming joint T-P filering.

        Instead of a T, E or B array, the filtering isotropic approximation is a 3x3 matrix.
    """


def get_response_sepTP_old(qe_key, lmax_qe, source, cls_weight, cls_cmb, fal_leg1, fal_leg2=None, lmax_out=None, ret_terms=False):
    #FIXME Curl lensign l=1 response non-zero
    lmax_source = lmax_qe # I think that's fine as long as we the same lmax on both legs.
    qes = get_qe_sepTP(qe_key, lmax_qe, cls_weight)
    resps = get_resp_legs(source, lmax_source)
    lmax_qlm= 2 * lmax_qe if lmax_out is None else lmax_out
    fal_leg2 = fal_leg1 if fal_leg2 is None else fal_leg2
    Rggcc = np.zeros((2, lmax_qlm+ 1), dtype=float)
    terms = []
    def _joincls(cls_list):
        lmaxp1 = np.min([len(cl) for cl in cls_list])
        return np.prod(np.array([cl[:lmaxp1] for cl in cls_list]), axis=0)
    for qe in qes: # loop over all quadratic terms in estimator
        si, ti = (qe.leg_a.spin_in, qe.leg_b.spin_in)
        so, to = (qe.leg_a.spin_ou, qe.leg_b.spin_ou)
        # Rst,r involves R^r, -ti}
        def add(si, ti, so, to, fla, flb):
            if np.all(fla == 0.) or np.all(flb == 0.):
                return np.zeros((2, lmax_qlm + 1), dtype=float)
            si = si * -1
            ti = ti * -1 # FIXME: why this sign flip here?
            cpling = get_coupling(si, -ti, cls_cmb)[:lmax_qe + 1]

            r, prR, mrR, s_cL = resps[-ti]  # There should always be a single term here.
            Rst_pr = get_hl(_joincls([prR, cpling, qe.leg_a.cl, fla]), _joincls([qe.leg_b.cl, flb]), ti - r, so, -ti, to, lmax_out=lmax_qlm) * s_cL[:lmax_qlm + 1]
            Rst_mr = get_hl(_joincls([mrR, cpling, qe.leg_a.cl, fla]), _joincls([qe.leg_b.cl, flb]), ti + r, so, -ti, to, lmax_out=lmax_qlm) * s_cL[:lmax_qlm + 1]
            # Swap s and t all over
            cpling *= (-1) ** (si - ti)
            r2, prR, mrR, s_cL = resps[-si]
            assert r2 == r, (r, r2)
            Rts_pr = get_hl(_joincls([prR, cpling, qe.leg_b.cl, flb]), _joincls([qe.leg_a.cl, fla]), si - r, to, -si, so, lmax_out=lmax_qlm) * s_cL[:lmax_qlm + 1]
            Rts_mr = get_hl(_joincls([mrR, cpling, qe.leg_b.cl, flb]), _joincls([qe.leg_a.cl, fla]), si + r, to, -si, so, lmax_out=lmax_qlm) * s_cL[:lmax_qlm + 1]
            gg = (Rst_mr + Rts_mr + (-1) ** r * (Rst_pr + Rts_pr)) * qe.cL[:lmax_qlm + 1]
            cc = (Rst_mr + Rts_mr - (-1) ** r * (Rst_pr + Rts_pr)) * qe.cL[:lmax_qlm + 1]
            terms.append(Rst_mr * qe.cL[:lmax_qlm + 1])
            terms.append(Rst_pr * qe.cL[:lmax_qlm + 1])
            terms.append(Rts_mr * qe.cL[:lmax_qlm + 1])
            terms.append(Rts_pr * qe.cL[:lmax_qlm + 1])
            return np.array([gg, cc])

        if si == 0 and ti == 0:
            Rggcc += add(si, ti, so, to, fal_leg1['t'], fal_leg2['t'])

        else:
            # Here we use _{\pm |s|}X = \pm^{s} 1/2 [ _{|s|} d_{lm}(f^g \pm f^c) _{|s|}d_{lm} + (-1)^{s} _{-|s|} d_{lm}(f^g \mp f^c) _{-|s|}d_{lm}
            #TODO: can simplify if one spin is zero
            sgs = 1 if si > 0 else (1 if abs(si)%2 == 0 else -1)
            sgt = 1 if ti > 0 else (1 if abs(ti)%2 == 0 else -1)

            prefac = 0.25 * sgs * sgt
            fla = fal_leg1['e'] + np.sign(si) * fal_leg1['b'] if abs(si) == 2 else fal_leg1['t']
            flb = fal_leg2['e'] + np.sign(ti) * fal_leg2['b'] if abs(ti) == 2 else fal_leg2['t']
            Rggcc += prefac * add(abs(si), abs(ti), so, to, fla, flb)

            fla = fal_leg1['e'] + np.sign(si) * fal_leg1['b'] if abs(si) == 2 else fal_leg1['t']
            flb = fal_leg2['e'] - np.sign(ti) * fal_leg2['b'] if abs(ti) == 2 else fal_leg2['t']
            Rggcc += (-1) ** ti *  prefac * add(abs(si), -abs(ti), so, to, fla, flb)

            fla = fal_leg1['e'] - np.sign(si) * fal_leg1['b'] if abs(si) == 2 else fal_leg1['t']
            flb = fal_leg2['e'] + np.sign(ti) * fal_leg2['b'] if abs(ti) == 2 else fal_leg2['t']
            Rggcc += (-1) ** si * prefac * add(-abs(si), abs(ti), so, to, fla, flb)

            fla = fal_leg1['e'] - np.sign(si) * fal_leg1['b'] if abs(si) == 2 else fal_leg1['t']
            flb = fal_leg2['e'] - np.sign(ti) * fal_leg2['b'] if abs(ti) == 2 else fal_leg2['t']
            Rggcc += (-1) ** (ti + si) * prefac * add(-abs(si), -abs(ti), so, to, fla, flb)
    return Rggcc if not ret_terms else (Rggcc, terms)


def get_response_sepTP(qe_key, lmax_qe, source, cls_weight, cls_cmb, fal_leg1,
                          fal_leg2=None, lmax_out=None):
    """
    Version based on cov-variations instead of field variation.
    """
    qes = get_qe_sepTP(qe_key, lmax_qe, cls_weight)
    lmax_qlm = min(2 * lmax_qe,  2 * lmax_qe if lmax_out is None else lmax_out)
    fal_leg2 = fal_leg1 if fal_leg2 is None else fal_leg2
    RGG = np.zeros(lmax_qlm + 1, dtype=float)
    RCC = np.zeros(lmax_qlm + 1, dtype=float)

    def get_F(s1, s2, leg):
        # Returns matrix element B^t Cov^{-1} in spin-space, for independ. T E B filtering (i.e. neglecting C_\ell^TE)).
        assert s1 in [0, -2, 2] and s2 in [0, -2, 2] and leg in [1, 2]
        fal = fal_leg1 if leg == 1 else fal_leg2
        if s1 == 0:
            return fal['t'] if s2 == 0 else None
        if s1 in [-2, 2]:
            if not s2 in [-2, 2]: return None
            return 0.5 * (fal['e'] + fal['b']) if s1 == s2 else 0.5 * (fal['e'] - fal['b'])
        else:
            assert 0

    for qe in qes:  # loop over all quadratic terms in estimator
        si, ti = (qe.leg_a.spin_in, qe.leg_b.spin_in)
        so, to = (qe.leg_a.spin_ou, qe.leg_b.spin_ou)
        # We want R^{a, st}  and R^{-a, st}
        for s2 in ([0] if si == 0 else [-2, 2]):
            FA = get_F(si, s2, 1)
            if FA is not None:
                for t2 in ([0] if ti == 0 else [-2, 2]):
                    FB = get_F(ti, t2, 2)
                    if FB is not None:
                        rW_st, prW_st, mrW_st, s_cL_st = get_covresp(source, -s2, t2, cls_cmb, len(FB) - 1)
                        clA = _joincls([qe.leg_a.cl, FA])
                        clB = _joincls([qe.leg_b.cl, FB, mrW_st])
                        Rpr_st = get_hl(clA, clB, so, s2, to, -s2 + rW_st, lmax_out=lmax_qlm) * s_cL_st[:lmax_qlm + 1]

                        rW_ts, prW_ts, mrW_ts, s_cL_ts = get_covresp(source, -t2, s2, cls_cmb, len(FA) - 1)
                        clA = _joincls([qe.leg_a.cl, FA, mrW_ts])
                        clB = _joincls([qe.leg_b.cl, FB])
                        Rpr_st += get_hl(clA, clB, so, -t2 + rW_ts, to, t2, lmax_out=lmax_qlm) * s_cL_ts[:lmax_qlm + 1]
                        assert rW_st == rW_ts and rW_st >= 0, (rW_st, rW_ts)
                        if rW_st > 0:
                            clA = _joincls([qe.leg_a.cl, FA])
                            clB = _joincls([qe.leg_b.cl, FB, prW_st])
                            Rmr_st = get_hl(clA, clB, so, s2, to, -s2 - rW_st, lmax_out=lmax_qlm) * s_cL_st[:lmax_qlm + 1]

                            clA = _joincls([qe.leg_a.cl, FA, prW_ts])
                            clB = _joincls([qe.leg_b.cl, FB])
                            Rmr_st += get_hl(clA, clB, so, -t2 - rW_ts, to, t2, lmax_out=lmax_qlm) * s_cL_ts[:lmax_qlm + 1]
                        else:
                            Rmr_st = Rpr_st
                        RGG += (-1) ** (so + to + rW_ts) * (Rpr_st + Rmr_st * (-1) ** rW_st) * qe.cL[:lmax_qlm + 1]
                        RCC += (-1) ** (so + to + rW_ts) * (Rpr_st - Rmr_st * (-1) ** rW_st) * qe.cL[:lmax_qlm + 1]
    return RGG, RCC


def get_nhl(qe_key1, qe_key2, cls_weights, cls_ivfs, lmax_qe, lmax_out=None, cls_ivfs_bb=None, cls_ivfs_ab=None):
    """(Semi-)Analytical noise level calculation.

    """
    qes1 = get_qe_sepTP(qe_key1, lmax_qe, cls_weights)
    qes2 = get_qe_sepTP(qe_key2, lmax_qe, cls_weights)
    lmax_out = 2 * lmax_qe if lmax_out is None else lmax_out
    G_N0 = np.zeros(lmax_out + 1, dtype=float)
    C_N0 = np.zeros(lmax_out + 1, dtype=float)
    cls_ivfs_aa = cls_ivfs
    cls_ivfs_bb = cls_ivfs if cls_ivfs_bb is None else cls_ivfs_bb
    cls_ivfs_ab = cls_ivfs if cls_ivfs_ab is None else cls_ivfs_ab
    cls_ivfs_ba = cls_ivfs_ab

    for qe1 in qes1:
        for qe2 in qes2:
            si, ti, ui, vi = (qe1.leg_a.spin_in, qe1.leg_b.spin_in, qe2.leg_a.spin_in, qe2.leg_b.spin_in)
            so, to, uo, vo = (qe1.leg_a.spin_ou, qe1.leg_b.spin_ou, qe2.leg_a.spin_ou, qe2.leg_b.spin_ou)
            assert so + to >= 0 and uo + vo >= 0, (so, to, uo, vo)
            sgn_R = (-1) ** (uo + vo + uo + vo)

            clsu = _joincls([qe1.leg_a.cl, qe2.leg_a.cl, get_coupling(si, ui, cls_ivfs_aa)])
            cltv = _joincls([qe1.leg_b.cl, qe2.leg_b.cl, get_coupling(ti, vi, cls_ivfs_bb)])
            R_sutv = sgn_R * _joincls(
                [get_hl(clsu, cltv, so, uo, to, vo, lmax_out=lmax_out), qe1.cL, qe2.cL])

            clsv = _joincls([qe1.leg_a.cl, qe2.leg_b.cl, get_coupling(si, vi, cls_ivfs_ab)])
            cltu = _joincls([qe1.leg_b.cl, qe2.leg_a.cl, get_coupling(ti, ui, cls_ivfs_ba)])
            R_sutv += sgn_R * _joincls(
                [get_hl(clsv, cltu, so, vo, to, uo, lmax_out=lmax_out), qe1.cL, qe2.cL])

            # we now need -s-t uv
            sgnms = (-1) ** (si + so)
            sgnmt = (-1) ** (ti + to)
            clsu = _joincls([sgnms * qe1.leg_a.cl, qe2.leg_a.cl, get_coupling(-si, ui, cls_ivfs_aa)])
            cltv = _joincls([sgnmt * qe1.leg_b.cl, qe2.leg_b.cl, get_coupling(-ti, vi, cls_ivfs_bb)])
            R_msmtuv = sgn_R * _joincls(
                [get_hl(clsu, cltv, -so, uo, -to, vo, lmax_out=lmax_out), qe1.cL, qe2.cL])

            clsv = _joincls([sgnms * qe1.leg_a.cl, qe2.leg_b.cl, get_coupling(-si, vi, cls_ivfs_ab)])
            cltu = _joincls([sgnmt * qe1.leg_b.cl, qe2.leg_a.cl, get_coupling(-ti, ui, cls_ivfs_ba)])
            R_msmtuv += sgn_R * _joincls(
                [get_hl(clsv, cltu, -so, vo, -to, uo, lmax_out=lmax_out), qe1.cL, qe2.cL])

            G_N0 +=  0.5 * R_sutv
            G_N0 +=  0.5 * (-1) ** (to + so) * R_msmtuv

            C_N0 += 0.5 * R_sutv
            C_N0 -= 0.5 * (-1) ** (to + so) * R_msmtuv
    return G_N0, C_N0


def get_mf_respv2(qe_key, cls_cmb, cls_ivfs, lmax_qe, lmax_out, ret_terms=None):
    print("Check accuracy not good enough at low-ell!")
    assert qe_key in ['p_p', 'ptt'], qe_key
    GL = np.zeros(lmax_out + 1, dtype=float)
    CL = np.zeros(lmax_out + 1, dtype=float)
    #GCL = np.zeros(lmax_out + 1, dtype=float)
    #CGL = np.zeros(lmax_out + 1, dtype=float)
    cst_term = 0.

    if qe_key == 'ptt':
        lmax_cmb = len(cls_cmb['tt']) - 1
        spins = [0]
    elif qe_key == 'p_p':
        lmax_cmb = min(len(cls_cmb['ee']) - 1, len(cls_cmb['bb'] - 1))
        spins = [-2, 2]
    elif qe_key == 'p':
        lmax_cmb = min(len(cls_cmb['ee']) - 1, len(cls_cmb['bb']) - 1, len(cls_cmb['tt']) - 1, len(cls_cmb['te']) - 1)
        spins = [0, -2, 2]
    else:
        assert 0, qe_key + ' not implemented'

    for s1 in spins:
        for s2 in spins:
            cl1 = get_coupling(s1, s2, cls_ivfs)[:lmax_qe + 1] * (0.5 ** (s1 != 0) * 0.5 ** (s2 != 0))
            # These 1/2 factor from the factor 1/2 in each B of B Covi B^dagger, where B maps spin-fields to T E B.
            cl2 = get_coupling(s2, s1, cls_cmb)[:lmax_cmb + 1]
            if np.any(cl1) and np.any(cl2):
                for a in [-1, 1]:
                    ai = get_alpha_lower(s2, lmax_cmb) if a == - 1 else get_alpha_raise(s2, lmax_cmb)
                    for b in [-1, 1]:
                        aj = get_alpha_lower(-s1, lmax_cmb) if b == 1 else get_alpha_raise(-s1, lmax_cmb)
                        hL = (-1) ** (s1 + s2) * get_hl(cl1, cl2 * ai * aj, s2, s1, -s2 - a, -s1 - b, lmax_out=lmax_out)
                        GL += (-1) * (1  if a == b else -1) * hL
                        CL += (-1) * hL
                        #GCL += (-1) * a * hL
                        #CGL += (-1) * b * hL

                        if a == b: # cst term
                            b1 =  get_alpha_lower(s1, lmax_qe) if a == -1 else get_alpha_raise(s1, lmax_qe)
                            b2  = get_alpha_lower(s1 + a, lmax_qe) if b == 1 else get_alpha_raise(s1 + a, lmax_qe)
                            cst_term += np.sum(cl1 * cl2[:lmax_qe+1] * b1 * b2 * (2 * np.arange(lmax_qe + 1) + 1)) * (-1) ** s1 /(4. * np.pi)

    print(-CL[1], cst_term)
    print(-CL[1] / cst_term - 1.)

    GL -= CL[1]
    CL -= CL[1]
    GL *= 0.25 * np.arange(lmax_out + 1) * np.arange(1, lmax_out + 2)
    CL *= 0.25 * np.arange(lmax_out + 1) * np.arange(1, lmax_out + 2)

    assert qe_key in ['ptt', 'p_p'],'FIXME: need MV (not sepTP quantities)'

    GLR, CLR = get_response_sepTP(qe_key, lmax_qe, 'p', cls_cmb, cls_cmb,
                                  {'t':cls_ivfs['tt'], 'e': cls_ivfs['ee'], 'b': cls_ivfs['bb']},lmax_out=lmax_out)
    GL -= GLR
    CL -= CLR
    return GL, CL, cst_term

def get_mf_resp(qe_key, cls_cmb, cls_ivfs, lmax_qe, lmax_out):
    """Deflection-induced mean-field response calculation.

    See Carron & Lewis 2019 in prep.
    """
    # This version looks stable enough
    assert qe_key in ['p_p', 'ptt'], qe_key
    GL = np.zeros(lmax_out + 1, dtype=float)
    CL = np.zeros(lmax_out + 1, dtype=float)
    if qe_key == 'ptt':
        lmax_cmb = len(cls_cmb['tt']) - 1
        spins = [0]
    elif qe_key == 'p_p':
        lmax_cmb = min(len(cls_cmb['ee']) - 1, len(cls_cmb['bb'] - 1))
        spins = [-2, 2]
    elif qe_key == 'p':
        lmax_cmb = min(len(cls_cmb['ee']) - 1, len(cls_cmb['bb']) - 1, len(cls_cmb['tt']) - 1, len(cls_cmb['te']) - 1)
        spins = [0, -2, 2]
    else:
        assert 0, qe_key + ' not implemented'
    assert lmax_qe <= lmax_cmb
    if qe_key == 'ptt':
        cl_cmbtoticmb = {'tt': cls_cmb['tt'][:lmax_qe + 1] ** 2 * cls_ivfs['tt'][:lmax_qe + 1]}
        cl_cmbtoti = {'tt': cls_cmb['tt'][:lmax_qe + 1] * cls_ivfs['tt'][:lmax_qe + 1]}
    elif qe_key == 'p_p':
        assert not np.any(cls_cmb['bb']), 'not implemented w. bb weights'
        cl_cmbtoticmb = {'ee': cls_cmb['ee'][:lmax_qe + 1] ** 2 * cls_ivfs['ee'][:lmax_qe + 1],
                         'bb': np.zeros(lmax_qe + 1, dtype=float)}
        cl_cmbtoti = {'ee': cls_cmb['ee'][:lmax_qe + 1] * cls_ivfs['ee'][:lmax_qe + 1],
                      'bb': np.zeros(lmax_qe + 1, dtype=float)}
    else:
        assert 0, 'not implemented'
    # Build remaining fisher term II:
    FisherGII = np.zeros(lmax_out + 1, dtype=float)
    FisherCII = np.zeros(lmax_out + 1, dtype=float)

    for s1 in spins:
        for s2 in spins:
            cl1 = get_coupling(s1, s2, cls_ivfs)[:lmax_qe + 1] * (0.5 ** (s1 != 0) * 0.5 ** (s2 != 0))
            # These 1/2 factor from the factor 1/2 in each B of B Covi B^dagger, where B maps spin-fields to T E B.
            cl2 = get_coupling(s2, s1, cls_cmb)[:lmax_cmb + 1]
            cl2[:lmax_qe + 1] -=  get_coupling(s2, s1, cl_cmbtoticmb)[:lmax_qe + 1]
            if np.any(cl1) and np.any(cl2):
                for a in [-1, 1]:
                    ai = get_alpha_lower(s2, lmax_cmb) if a == - 1 else get_alpha_raise(s2, lmax_cmb)
                    for b in [1]: # a, b symmetry
                        fac = 2
                        aj = get_alpha_lower(-s1, lmax_cmb) if b == 1 else get_alpha_raise(-s1, lmax_cmb)
                        hL = fac * (-1) ** (s1 + s2) * get_hl(cl1, cl2 * ai * aj, s2, s1, -s2 - a, -s1 - b, lmax_out=lmax_out)
                        GL += (- a * b) * hL
                        CL += (-1) * hL

    # Build remaining Fisher term II:
    for s1 in spins:
        for s2 in spins:
            cl1 = get_coupling(s2, s1, cl_cmbtoti)[:lmax_qe + 1] * (0.5 ** (s1 != 0))
            cl2 = get_coupling(s1, s2, cl_cmbtoti)[:lmax_qe + 1] * (0.5 ** (s2 != 0))
            if np.any(cl1) and np.any(cl2):
                for a in [-1, 1]:
                    ai = get_alpha_lower(s2, lmax_qe) if a == -1 else get_alpha_raise(s2, lmax_qe)
                    for b in [1]:
                        fac = 2
                        aj = get_alpha_lower(s1, lmax_qe) if b == 1 else get_alpha_raise(s1, lmax_qe)
                        hL = fac * (-1) ** (s1 + s2) * get_hl(cl1 * ai, cl2 * aj, -s2 - a, -s1, s2, s1 -b, lmax_out=lmax_out)
                        FisherGII += (- a * b) * hL
                        FisherCII += (-1) * hL
    GL -= FisherGII
    CL -= FisherCII
    print("CL[1] ",CL[1])
    print("GL[1] (before subtraction) ", GL[1])
    print("GL[1] (after subtraction) ", GL[1] - CL[1])

    GL -= CL[1]
    CL -= CL[1]
    GL *= 0.25 * np.arange(lmax_out + 1) * np.arange(1, lmax_out + 2)
    CL *= 0.25 * np.arange(lmax_out + 1) * np.arange(1, lmax_out + 2)
    return GL, CL

GL_cache = {}

def get_hl(cl1, cl2, sp1, s1, sp2, s2, lmax_out=None):
    """Legendre coeff. of $ (\\xi_{sp1,s1} * \\xi_{sp2,s2})(\\cos \\theta)$ from their harmonic series.

        The integrand is always a polynomial, of max. degree lmax1 + lmax2 + lmax_out.
        We use Gauss-Legendre integration to solve this exactly.
    """
    lmax1 = len(cl1) - 1
    lmax2 = len(cl2) - 1
    lmax_out = lmax1 + lmax2 if lmax_out is None else lmax_out
    lmaxtot = lmax1 + lmax2 + lmax_out
    N = (lmaxtot + 2 - lmaxtot % 2) // 2
    if not 'xg wg %s' % N in GL_cache.keys():
        GL_cache['xg wg %s' % N] = wigners.get_xgwg(-1., 1., N) if HASWIGNER else gauleg.get_xgwg(N)
    xg, wg = GL_cache['xg wg %s' % N]

    if HASWIGNER:
        xi1 = wigners.wignerpos(cl1, xg, sp1, s1)
        xi2 = wigners.wignerpos(cl2, xg, sp2, s2)
        return wigners.wignercoeff(xi1 * xi2 * wg, xg, sp1 + sp2, s1 + s2, lmax_out)
    else:
        xi1 = gaujac.get_rspace(cl1, xg, sp1, s1)
        xi2 = gaujac.get_rspace(cl2, xg, sp2, s2)
        return 2. * np.pi * np.dot(gaujac.get_wignerd(lmax_out, xg, sp1 + sp2, s1 + s2), wg * xi1 * xi2)


def get_alpha_raise(s, lmax):
    """Response coefficient of spin-s spherical harmonic to spin raising operator.

        +\sqrt{ (l - s) (l + s + 1) } for abs(s) <= l <= lmax

    """
    ret = np.zeros(lmax + 1, dtype=float)
    ret[abs(s):] = np.sqrt(np.arange(abs(s) -s, lmax - s + 1) * np.arange(abs(s) + s + 1, lmax + s + 2))
    return ret

def get_alpha_lower(s, lmax):
    """Response coefficient of spin-s spherical harmonic to spin lowering operator.

        -\sqrt{ (l + s) (l - s + 1) } for abs(s) <= l <= lmax

    """
    ret = np.zeros(lmax + 1, dtype=float)
    ret[abs(s):] = -np.sqrt(np.arange(s + abs(s), lmax + s + 1) * np.arange(abs(s) - s + 1, lmax - s + 2))
    return ret

def get_lensing_resp(s, lmax):
    """ -1/2 1d eth X - 1/2 -1d eth X """
    return  {1: -0.5 * get_alpha_lower(s, lmax), -1: -0.5 * get_alpha_raise(s, lmax)}

def get_coupling(s1, s2, cls):
    """<_{s1}X_{lm} _{s2}X^*{lm}>

    Note:
        This uses the spin-field conventions where _0X_{lm} = -T_{lm}

    """
    if s1 < 0:
        return (-1) ** (s1 + s2) * get_coupling(-s1, -s2, cls)
    assert s1 in [0, -2, 2] and s2 in [0, -2, 2], (s1, s2 , 'not implemented')
    if s1 == 0 :
        if s2 == 0 :
            return cls['tt'].copy()
        return -cls['te'].copy()
    elif s1 == 2:
        if s2 == 0:
            return -cls['te'].copy()
        return cls['ee'] + np.sign(s2) * cls['bb']
