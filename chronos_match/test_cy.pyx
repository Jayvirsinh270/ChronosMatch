# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False

cdef extern from "chronos_types.h":
    ctypedef struct OrderPacket:
        unsigned long long order_id
        long long price
        unsigned int quantity

cpdef long long fast_multiply(long long price, unsigned int qty) noexcept nogil:
    return price * qty
