# This file is part of PyEMMA.
#
# Copyright (c) 2015, 2014 Computational Molecular Biology Group, Freie Universitaet Berlin (GER)
#
# PyEMMA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
'''
Created on 19.01.2015

@author: marscher
'''

from __future__ import absolute_import

from math import log

import numpy as np
import scipy.linalg as scl
from decorator import decorator
from pyemma._base.model import Model
from pyemma.coordinates.estimation.covariance import CovarEstimator
from pyemma.coordinates.estimation.koopman import _KoopmanEstimator
from pyemma.coordinates.data._base.transformer import StreamingEstimationTransformer
from pyemma.util.annotators import fix_docs, deprecated
from pyemma._ext.variational.solvers.direct import eig_corr
from pyemma._ext.variational.solvers.direct import sort_by_norm
from pyemma.util.reflection import get_default_args


__all__ = ['TICA', 'EquilibriumCorrectedTICA']


class TICAModel(Model):
    def set_model_params(self, mean, cov, cov_tau):
        self.mean = mean
        self.cov = cov
        self.cov_tau = cov_tau


@decorator
def _lazy_estimation(func, *args, **kw):
    assert isinstance(args[0], TICA)
    tica_obj = args[0]
    if not tica_obj._estimated:
        tica_obj._diagonalize()
    return func(*args, **kw)


class _TICA(StreamingEstimationTransformer):
    r""" Time-lagged independent component analysis (TICA)"""

    def __init__(self, lag, dim=-1, var_cutoff=0.95, kinetic_map=True, commute_map=False, epsilon=1e-6,
                 mean=None, stride=1, remove_mean=True, skip=0, reversible=True):
        r""" Time-lagged independent component analysis (TICA) [1]_, [2]_, [3]_.

        Parameters
        ----------
        lag : int
            lag time
        dim : int, optional, default -1
            Maximum number of significant independent components to use to reduce dimension of input data. -1 means
            all numerically available dimensions (see epsilon) will be used unless reduced by var_cutoff.
            Setting dim to a positive value is exclusive with var_cutoff.
        var_cutoff : float in the range [0,1], optional, default 0.95
            Determines the number of output dimensions by including dimensions until their cumulative kinetic variance
            exceeds the fraction subspace_variance. var_cutoff=1.0 means all numerically available dimensions
            (see epsilon) will be used, unless set by dim. Setting var_cutoff smaller than 1.0 is exclusive with dim
        kinetic_map : bool, optional, default True
            Eigenvectors will be scaled by eigenvalues. As a result, Euclidean distances in the transformed data
            approximate kinetic distances [4]_. This is a good choice when the data is further processed by clustering.
        commute_map : bool, optional, default False
            Eigenvector_i will be scaled by sqrt(timescale_i / 2). As a result, Euclidean distances in the transformed
            data will approximate commute distances [5]_.
        epsilon : float
            eigenvalue norm cutoff. Eigenvalues of C0 with norms <= epsilon will be
            cut off. The remaining number of eigenvalues define the size
            of the output.
        mean : ndarray, optional, default None
            This option is deprecated
        remove_mean: bool, optional, default True
            remove mean during covariance estimation. Should not be turned off.
        skip : int, default=0
            skip the first initial n frames per trajectory.

        Notes
        -----
        Given a sequence of multivariate data :math:`X_t`, computes the mean-free
        covariance and time-lagged covariance matrix:

        .. math::

            C_0 &=      (X_t - \mu)^T (X_t - \mu) \\
            C_{\tau} &= (X_t - \mu)^T (X_{t + \tau} - \mu)

        and solves the eigenvalue problem

        .. math:: C_{\tau} r_i = C_0 \lambda_i(tau) r_i,

        where :math:`r_i` are the independent components and :math:`\lambda_i(tau)` are
        their respective normalized time-autocorrelations. The eigenvalues are
        related to the relaxation timescale by

        .. math:: t_i(tau) = -\tau / \ln |\lambda_i|.

        When used as a dimension reduction method, the input data is projected
        onto the dominant independent components.

        References
        ----------
        .. [1] Perez-Hernandez G, F Paul, T Giorgino, G De Fabritiis and F Noe. 2013.
           Identification of slow molecular order parameters for Markov model construction
           J. Chem. Phys. 139, 015102. doi:10.1063/1.4811489
        .. [2] Schwantes C, V S Pande. 2013.
           Improvements in Markov State Model Construction Reveal Many Non-Native Interactions in the Folding of NTL9
           J. Chem. Theory. Comput. 9, 2000-2009. doi:10.1021/ct300878a
        .. [3] L. Molgedey and H. G. Schuster. 1994.
           Separation of a mixture of independent signals using time delayed correlations
           Phys. Rev. Lett. 72, 3634.
        .. [4] Noe, F. and Clementi, C. 2015. Kinetic distance and kinetic maps from molecular dynamics simulation.
            J. Chem. Theory. Comput. doi:10.1021/acs.jctc.5b00553
        .. [5] Noe, F., Banisch, R., Clementi, C. 2016. Commute maps: separating slowly-mixing molecular configurations
           for kinetic modeling. J. Chem. Theory. Comput. doi:10.1021/acs.jctc.6b00762

        """
        default_var_cutoff = get_default_args(self.__init__)['var_cutoff']
        if dim != -1 and var_cutoff != default_var_cutoff:
            raise ValueError('Trying to set both the number of dimension and the subspace variance. Use either or.')
        if kinetic_map and commute_map:
            raise ValueError('Trying to use both kinetic_map and commute_map. Use either or.')
        if (kinetic_map or commute_map) and not reversible:
            raise NotImplementedError('kinetic_map and commute_map are not yet implemented for irreversible processes.')
        super(_TICA, self).__init__()

        if dim > -1:
            var_cutoff = 1.0

        self._covar = CovarEstimator(xx=True, xy=True, yy=False, remove_data_mean=remove_mean, reversible=reversible,
                                     lag=lag, stride=stride, skip=skip)

        # empty dummy model instance
        self._model = TICAModel()
        self.set_params(lag=lag, dim=dim, var_cutoff=var_cutoff, kinetic_map=kinetic_map, commute_map=commute_map,
                        epsilon=epsilon, mean=mean, remove_mean=remove_mean, reversible=reversible, stride=stride, skip=skip)

    @property
    def lag(self):
        """ lag time of correlation matrix :math:`C_{\tau}` """
        return self._lag

    @lag.setter
    def lag(self, new_tau):
        self._lag = new_tau

    def describe(self):
        try:
            dim = self.dimension()
        except AttributeError:
            dim = self.dim
        return "[TICA, lag = %i; max. output dim. = %i]" % (self._lag, dim)

    def dimension(self):
        """ output dimension """
        if self.dim > -1:
            return self.dim
        d = None
        if self.dim != -1 and not self._estimated:  # fixed parametrization
            d = self.dim
        elif self._estimated:  # parametrization finished. Dimension is known
            dim = len(self.eigenvalues)
            if self.var_cutoff < 1.0:  # if subspace_variance, reduce the output dimension if needed
                dim = min(dim, np.searchsorted(self.cumvar, self.var_cutoff) + 1)
            d = dim
        elif self.var_cutoff == 1.0:  # We only know that all dimensions are wanted, so return input dim
            d = self.data_producer.dimension()
        else:  # We know nothing. Give up
            raise RuntimeError('Requested dimension, but the dimension depends on the cumulative variance and the '
                               'transformer has not yet been estimated. Call estimate() before.')
        return d

    @property
    def mean(self):
        """ mean of input features """
        return self._model.mean

    @property
    @deprecated('please use the "mean" property')
    def mu(self):
        """DEPRECATED: please use the "mean" property"""
        return self.mean

    @mean.setter
    def mean(self, value):
        self._model.mean = value

    def estimate(self, X, **kwargs):
        r"""
        Chunk-based parameterization of TICA. Iterates over all data and estimates
        the mean, covariance and time lagged covariance. Finally, the
        generalized eigenvalue problem is solved to determine
        the independent components.
        """
        return super(_TICA, self).estimate(X, **kwargs)

    @property
    def timescales(self):
        r"""Implied timescales of the TICA transformation

        For each :math:`i`-th eigenvalue, this returns

        .. math::

            t_i = -\frac{\tau}{\log(|\lambda_i|)}

        where :math:`\tau` is the :py:obj:`lag` of the TICA object and :math:`\lambda_i` is the `i`-th
        :py:obj:`eigenvalue <eigenvalues>` of the TICA object.

        Returns
        -------
        timescales: 1D np.array
            numpy array with the implied timescales. In principle, one should expect as many timescales as
            input coordinates were available. However, less eigenvalues will be returned if the TICA matrices
            were not full rank or :py:obj:`var_cutoff` was parsed
        """
        return -self.lag / np.log(np.abs(self.eigenvalues))

    def output_type(self):
        # TODO: handle the case of conjugate pairs
        if np.all(np.isreal(self.eigenvectors[:, 0:self.dimension()])) or \
            np.allclose(np.imag(self.eigenvectors[:, 0:self.dimension()]), 0):
            return super(_TICA, self).output_type()
        else:
            return np.complex64

    # TODO
    #@property
    #@_lazy_estimation
    #def koopman_matrix(self):
    #    pass

@fix_docs
class TICA(_TICA):
    def partial_fit(self, X):
        """ incrementally update the covariances and mean.

        Parameters
        ----------
        X: array, list of arrays, PyEMMA reader
            input data.

        Notes
        -----
        The projection matrix is first being calculated upon its first access.
        """
        from pyemma.coordinates import source
        iterable = source(X)

        indim = iterable.dimension()
        if not self.dim <= indim:
            raise RuntimeError("requested more output dimensions (%i) than dimension"
                               " of input data (%i)" % (self.dim, indim))

        self._covar.partial_fit(iterable)
        self._model.update_model_params(mean=self._covar.mean,  # TODO: inefficient, fixme
                                        cov=self._covar.cov,
                                        cov_tau=self._covar.cov_tau)

        self._used_data = self._covar._used_data
        self._estimated = False

        return self

    def _estimate(self, iterable, **kw):
        indim = iterable.dimension()

        if not self.dim <= indim:
            raise RuntimeError("requested more output dimensions (%i) than dimension"
                               " of input data (%i)" % (self.dim, indim))

        if self._logger_is_active(self._loglevel_DEBUG):
            self._logger.debug("Running TICA with tau=%i; Estimating two covariance matrices"
                               " with dimension (%i, %i)" % (self._lag, indim, indim))

        self._covar.estimate(iterable, **kw)
        self._model.update_model_params(mean=self._covar.mean,
                                        cov=self._covar.cov,
                                        cov_tau=self._covar.cov_tau)
        self._diagonalize()

        return self._model

    def _diagonalize(self):
        # diagonalize with low rank approximation
        self._logger.debug("diagonalize Cov and Cov_tau.")

        eigenvalues, eigenvectors = eig_corr(self._covar.cov, self.cov_tau, self.epsilon)
        self._logger.debug("finished diagonalisation.")

        # compute cumulative variance
        cumvar = np.cumsum(np.abs(eigenvalues) ** 2)
        cumvar /= cumvar[-1]

        self._model.update_model_params(cumvar=cumvar,
                                        eigenvalues=eigenvalues,
                                        eigenvectors=eigenvectors)

        self._estimated = True

    def _transform_array(self, X):
        r"""Projects the data onto the dominant independent components.

        Parameters
        ----------
        X : ndarray(n, m)
            the input data

        Returns
        -------
        Y : ndarray(n,)
            the projected data
        """
        X_meanfree = X - self.mean
        Y = np.dot(X_meanfree, self.eigenvectors[:, 0:self.dimension()])
        if self.kinetic_map and self.commute_map:
            raise ValueError('Trying to use both kinetic_map and commute_map. Use either or.')
        if self.kinetic_map:  # scale by eigenvalues
            Y *= self.eigenvalues[0:self.dimension()]
        if self.commute_map:  # scale by (regularized) timescales
            timescales = self.timescales[0:self.dimension()]

            # dampen timescales smaller than the lag time, as in section 2.5 of ref. [5]
            regularized_timescales = 0.5 * timescales * np.tanh(np.pi * ((timescales - self.lag) / self.lag) + 1)

            Y *= np.sqrt(regularized_timescales / 2)
        return Y.astype(self.output_type())

    @property
    def feature_TIC_correlation(self):
        r"""Instantaneous correlation matrix between input features and TICs

        Denoting the input features as :math:`X_i` and the TICs as :math:`\theta_j`, the instantaneous, linear correlation
        between them can be written as

        .. math::

            \mathbf{Corr}(X_i, \mathbf{\theta}_j) = \frac{1}{\sigma_{X_i}}\sum_l \sigma_{X_iX_l} \mathbf{U}_{li}

        The matrix :math:`\mathbf{U}` is the matrix containing, as column vectors, the eigenvectors of the TICA
        generalized eigenvalue problem .

        Returns
        -------
        feature_TIC_correlation : ndarray(n,m)
            correlation matrix between input features and TICs. There is a row for each feature and a column
            for each TIC.
        """
        feature_sigma = np.sqrt(np.diag(self.cov))
        return np.dot(self.cov, self.eigenvectors[:, : self.dimension()]) / feature_sigma[:, np.newaxis]

    @property
    def cov(self):
        """ covariance matrix of input data. """
        return self._model.cov

    @cov.setter
    def cov(self, value):
        self._model.cov = value

    @property
    def cov_tau(self):
        """ covariance matrix of time-lagged input data. """
        return self._model.cov_tau

    @cov_tau.setter
    def cov_tau(self, value):
        self._model.cov_tau = value

    @property
    @_lazy_estimation
    def eigenvalues(self):
        r"""Eigenvalues of the TICA problem (usually denoted :math:`\lambda`

        Returns
        -------
        eigenvalues: 1D np.array
        """
        return self._model.eigenvalues

    @property
    @_lazy_estimation
    def eigenvectors(self):
        r"""Eigenvectors of the TICA problem, columnwise

        Returns
        -------
        eigenvectors: (N,M) ndarray
        """
        return self._model.eigenvectors

    @property
    @_lazy_estimation
    def cumvar(self):
        r"""Cumulative sum of the the TICA eigenvalues

        Returns
        -------
        cumvar: 1D np.array
        """
        return self._model.cumvar


@fix_docs
class EquilibriumCorrectedTICA(_TICA):
    def _estimate(self, iterable, **kwargs):
        koop = _KoopmanEstimator(lag=self.lag, epsilon=self.epsilon, stride=self.stride, skip=self.skip)
        koop.estimate(iterable, **kwargs)
        K = koop.K
        R = koop.R
        r = R.shape[1]

        x_mean_0 = koop.mean

        self._covar = CovarEstimator(lag=self.lag, weights=koop.weights, remove_constant_mean=x_mean_0, xy=False,
                                     remove_data_mean=False, reversible=self.reversible, stride=self.stride,
                                     skip=self.skip)
        self._covar.estimate(iterable, **kwargs)
        C0 = self._covar.cov

        self._mean_pc_1 = np.concatenate((self._covar.mean.dot(R), [1.0])) # for testing

        C_0_eq = np.zeros(shape=(r+1, r+1)) # in modified basis (PC|1)
        C_0_eq[0:r,0:r] = R.T.dot(C0).dot(R)
        C_0_eq[0:r, r] = self._covar.mean.dot(R)
        C_0_eq[r, 0:r] = self._covar.mean.dot(R)
        C_0_eq[r,r] = 1.0
        self._cov_pc_1 = C_0_eq # for testing
        C_tau_eq = K
        # find R_eq s.t. R_eq.T.dot(C_0_eq).dot(R_eq) = np.eye(s)
        s, Q = scl.eigh(C_0_eq)
        evmin = np.min(s)
        if evmin < 0:
            ep0 = np.maximum(self.epsilon, -evmin)
        else:
            ep0 = self.epsilon
        s, Q = sort_by_norm(s, Q)
        ind = np.where(np.abs(s) > ep0)[0]
        s = s[ind]
        Q = Q[:, ind]
        R_eq = np.dot(Q, np.diag(s ** -0.5))
        # Compute equilibrium K:
        K_eq = 0.5 * R_eq.T.dot(C_0_eq.dot(K) + K.T.dot(C_0_eq)).dot(R_eq)
        self._cov_tau_pc_1 = K_eq # for testing
        # Diagonalize K_eq:
        d, V = scl.eigh(K_eq)
        d, V = sort_by_norm(d, V)
        W = R_eq.dot(V)
        self._tr = R.dot(W[0:r, :])
        self._tr_c = W[r, :] - x_mean_0.T.dot(R).dot(W[0:r, :])

        # update model parameters
        eigenvalues = d
        cumvar = np.cumsum(np.abs(eigenvalues) ** 2)
        cumvar /= cumvar[-1]

        self._model.update_model_params(mean=self._covar.mean,
                                        cumvar=cumvar,
                                        eigenvalues=eigenvalues)
        self._estimated = True
        return self

    def _transform_array(self, X):
        return X.dot(self._tr) + self._tr_c

    @property
    def koopman_matrix(self):
        pass

    @property
    def eigenvalues(self):
        r"""Eigenvalues of the TICA problem (usually denoted :math:`\lambda`

        Returns
        -------
        eigenvalues: 1D np.array
        """
        return self._model.eigenvalues

    @property
    def cumvar(self):
        r"""Cumulative sum of the the TICA eigenvalues

        Returns
        -------
        cumvar: 1D np.array
        """
        return self._model.cumvar
