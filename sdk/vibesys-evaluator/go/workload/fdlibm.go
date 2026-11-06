package workload

import "math"

// fdlibm's e_log.c and e_exp.c, for finite x > 0 and finite x, as the Python SDK's Rand computes
// them (vibesys_evaluator/_private/rand.py): the same IEEE 754 operations in the same order.
// Every product is converted explicitly so that no compiler fuses a multiply and an add (the Go
// spec allows fusion otherwise), which would change the last bit.

const (
	ln2Hi        = 6.93147180369123816490e-01
	ln2Lo        = 1.90821492927058770002e-10
	log2e        = 1.44269504088896338700e+00
	sqrtHalf     = 7.07106781186547524401e-01
	expOverflow  = 7.09782712893383973096e+02
	expUnderflow = -7.45133219101941108420e+02
	nearZero     = 1.0 / (1 << 28)

	lg1 = 6.666666666666735130e-01
	lg2 = 3.999999999940941908e-01
	lg3 = 2.857142874366239149e-01
	lg4 = 2.222219843214978396e-01
	lg5 = 1.818357216161805012e-01
	lg6 = 1.531383769920937332e-01
	lg7 = 1.479819860511658591e-01

	p1 = 1.66666666666666657415e-01
	p2 = -2.77777777770155933842e-03
	p3 = 6.61375632143793436117e-05
	p4 = -1.65339022054652515390e-06
	p5 = 4.13813679705723846039e-08
)

func mul(a, b float64) float64 { return float64(a * b) }

func fdlibmLog(x float64) float64 {
	f1, ki := math.Frexp(x)
	if f1 < sqrtHalf {
		f1 = mul(f1, 2)
		ki--
	}
	f := f1 - 1
	k := float64(ki)
	s := f / (2 + f)
	s2 := mul(s, s)
	s4 := mul(s2, s2)
	t1 := mul(s2, lg1+mul(s4, lg3+mul(s4, lg5+mul(s4, lg7))))
	t2 := mul(s4, lg2+mul(s4, lg4+mul(s4, lg6)))
	r := t1 + t2
	hfsq := mul(mul(0.5, f), f)
	return mul(k, ln2Hi) - ((hfsq - (mul(s, hfsq+r) + mul(k, ln2Lo))) - f)
}

func fdlibmExp(x float64) float64 {
	switch {
	case x > expOverflow:
		return math.Inf(1)
	case x < expUnderflow:
		return 0
	case -nearZero < x && x < nearZero:
		return 1 + x
	}
	var k int
	if x < 0 {
		k = int(mul(log2e, x) - 0.5)
	} else {
		k = int(mul(log2e, x) + 0.5)
	}
	hi := x - mul(float64(k), ln2Hi)
	lo := mul(float64(k), ln2Lo)
	r := hi - lo
	t := mul(r, r)
	c := r - mul(t, p1+mul(t, p2+mul(t, p3+mul(t, p4+mul(t, p5)))))
	y := 1 - ((lo - mul(r, c)/(2-c)) - hi)
	return math.Ldexp(y, k)
}

// fsum is the exactly rounded sum of xs, as Python's math.fsum computes it (Shewchuk's partials,
// then the round-half-even correction), for finite values.
func fsum(xs []float64) float64 {
	partials := make([]float64, 0, 8)
	for _, x := range xs {
		i := 0
		for _, y := range partials {
			if math.Abs(x) < math.Abs(y) {
				x, y = y, x
			}
			hi := x + y
			lo := y - (hi - x)
			if lo != 0 {
				partials[i] = lo
				i++
			}
			x = hi
		}
		partials = partials[:i]
		if x != 0 {
			partials = append(partials, x)
		}
	}
	n := len(partials)
	hi, lo := 0.0, 0.0
	if n > 0 {
		n--
		hi = partials[n]
		for n > 0 {
			x := hi
			n--
			y := partials[n]
			hi = x + y
			lo = y - (hi - x)
			if lo != 0 {
				break
			}
		}
		if n > 0 && ((lo < 0 && partials[n-1] < 0) || (lo > 0 && partials[n-1] > 0)) {
			y := lo * 2
			x := hi + y
			if y == x-hi {
				hi = x
			}
		}
	}
	return hi
}
