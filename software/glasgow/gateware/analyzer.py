from functools import reduce
from collections import OrderedDict
from migen import *
from migen.fhdl.bitcontainer import log2_int
from migen.genlib.fifo import _FIFOInterface, SyncFIFOBuffered
from migen.genlib.coding import PriorityEncoder, PriorityDecoder
from migen.genlib.fsm import FSM


__all__ = ["EventSource", "EventAnalyzer", "TraceDecodingError", "TraceDecoder"]


REPORT_DELAY      = 0b10000000
REPORT_DELAY_MASK = 0b10000000
REPORT_EVENT      = 0b01000000
REPORT_EVENT_MASK = 0b11000000
REPORT_DONE       = 0b00000000
REPORT_DONE_MASK  = 0b11000000


class EventSource(Module):
    def __init__(self, name, width, fields, depth):
        self.name    = name
        self.width   = width
        self.fields  = fields
        self.depth   = depth

        self.data    = Signal(max(1, width))
        self.trigger = Signal()


class EventAnalyzer(Module):
    """
    An event analyzer module.

    This event analyzer is designed to observe parallel, bursty processes in real-time, and yet
    degrade gracefully (i.e. without losing data or breaking most applets) when observing processes
    that generate events continuously, or generate very many simultaneous events for a short time.
    To do this, the event analyzer is permitted to pause any applets marked as purely synchronous
    once the event FIFO high-water mark is reached.

    The event analyzer tries to make efficient use of power-of-2 wide block RAMs and be highly
    tunable. To achieve this, it separates the event FIFO from the event data FIFOs, and does not
    storing timestamps explicitly. In a system with `n` events, each of which carries `d_n` bits
    of data, there would be a single event FIFO that is `n` bits wide, where a bit being set means
    that event `n` occurred at a given cycle; `n` event data FIFOs that are `d_n` bits wide each,
    where, if a bit is set in the event FIFO, a data word is pushed into the event data FIFO; and
    finally, one delay FIFO, where the last entry is incremented on every cycle that has
    no event, and a new entry is pushed on every cycle there is at least one event. This way,
    only cycles that have at least one event add new FIFO entries, and only one wide timestamp
    counter needs to be maintained, greatly reducing the amount of necessary resources compared
    to a more naive approach.
    """

    @staticmethod
    def _depth_for_width(width):
        if width == 0:
            return 0
        elif width <= 2:
            return 2048
        elif width <= 4:
            return 1024
        elif width <= 8:
            return 512
        else:
            return 256

    def __init__(self, output_fifo, event_depth=None, delay_width=16):
        assert output_fifo.width == 8

        self.output_fifo   = output_fifo
        self.delay_width   = delay_width
        self.event_depth   = event_depth
        self.event_sources = Array()
        self.done          = Signal()

    def add_event_source(self, name, width=0, fields=(), depth=None):
        if depth is None:
            depth = self._depth_for_width(width)

        event_source = EventSource(name, width, fields, depth)
        self.event_sources.append(event_source)
        return event_source

    def do_finalize(self):
        assert len(self.event_sources) < 2 ** 6
        assert max(s.width for s in self.event_sources) <= 32

        # Fill the event, event data, and delay FIFOs.
        if self.event_depth is None:
            event_depth = min(self._depth_for_width(len(self.event_sources)),
                              self._depth_for_width(self.delay_width))
        else:
            event_depth = self.event_depth

        self.submodules.event_fifo = event_fifo = \
            SyncFIFOBuffered(width=len(self.event_sources), depth=event_depth)
        self.comb += [
            event_fifo.din.eq(Cat([s.trigger for s in self.event_sources])),
            event_fifo.we.eq(reduce(lambda a, b: a | b, (s.trigger for s in self.event_sources)))
        ]

        self.submodules.delay_fifo = delay_fifo = \
            SyncFIFOBuffered(width=self.delay_width, depth=event_depth)
        delay_timer = self._delay_timer = Signal(self.delay_width, reset=1)
        self.sync += [
            If(delay_fifo.we,
                delay_timer.eq(1)
            ).Else(
                delay_timer.eq(delay_timer + 1)
            )
        ]
        self.comb += [
            delay_fifo.din.eq(delay_timer),
            delay_fifo.we.eq(event_fifo.we | (delay_timer == ((1 << self.delay_width) - 1))),
        ]

        for event_source in self.event_sources:
            if event_source.width > 0:
                event_source.submodules.data_fifo = event_data_fifo = \
                    SyncFIFOBuffered(event_source.width, event_source.depth)
                self.submodules += event_source
                self.comb += [
                    event_data_fifo.din.eq(event_source.data),
                    event_data_fifo.we.eq(event_source.trigger),
                ]
            else:
                event_source.submodules.data_fifo = _FIFOInterface(1, 0)

        # Dequeue events, and serialize events and event data.
        self.submodules.event_encoder = event_encoder = \
            PriorityEncoder(width=len(self.event_sources))
        self.submodules.event_decoder = event_decoder = \
            PriorityDecoder(width=len(self.event_sources))
        self.comb += event_decoder.i.eq(event_encoder.o)

        self.submodules.serializer = serializer = FSM(reset_state="WAIT-EVENT")
        delay_septets = 5
        delay_counter = Signal(7 * delay_septets)
        serializer.act("WAIT-EVENT",
            If(delay_fifo.readable,
                delay_fifo.re.eq(1),
                NextValue(delay_counter, delay_counter + delay_fifo.dout),
            ),
            If(event_fifo.readable,
                event_fifo.re.eq(1),
                NextValue(event_encoder.i, event_fifo.dout),
                If(event_fifo.dout != 0,
                    NextState("REPORT-DELAY")
                )
            ).Elif(self.done,
                NextState("REPORT-DONE")
            )
        )
        serializer.act("REPORT-DELAY",
            If(delay_counter >= 128 ** 4,
                NextState("REPORT-DELAY-5")
            ).Elif(delay_counter >= 128 ** 3,
                NextState("REPORT-DELAY-4")
            ).Elif(delay_counter >= 128 ** 2,
                NextState("REPORT-DELAY-3")
            ).Elif(delay_counter >= 128 ** 1,
                NextState("REPORT-DELAY-2")
            ).Else(
                NextState("REPORT-DELAY-1")
            )
        )
        for septet_no in range(delay_septets, 0, -1):
            if septet_no == 1:
                next_state = [
                    NextValue(delay_counter, 0),
                    NextState("REPORT-EVENT")
                ]
            else:
                next_state = [
                    NextState("REPORT-DELAY-%d" % (septet_no - 1))
                ]
            serializer.act("REPORT-DELAY-%d" % septet_no,
                If(self.output_fifo.writable,
                    self.output_fifo.din.eq(
                        REPORT_DELAY | delay_counter.part((septet_no - 1) * 7, 7)),
                    self.output_fifo.we.eq(1),
                    *next_state
                )
            )
        event_source = self.event_sources[event_encoder.o]
        event_data   = Signal(32)
        serializer.act("REPORT-EVENT",
            If(self.output_fifo.writable,
                NextValue(event_encoder.i, event_encoder.i & ~event_decoder.o),
                self.output_fifo.din.eq(
                    REPORT_EVENT | event_encoder.o),
                self.output_fifo.we.eq(1),
                NextValue(event_data, event_source.data_fifo.dout),
                event_source.data_fifo.re.eq(1),
                If(event_source.width > 24,
                    NextState("REPORT-EVENT-DATA-4")
                ).Elif(event_source.width > 16,
                    NextState("REPORT-EVENT-DATA-3")
                ).Elif(event_source.width > 8,
                    NextState("REPORT-EVENT-DATA-2")
                ).Elif(event_source.width > 0,
                    NextState("REPORT-EVENT-DATA-1")
                ).Else(
                    If(event_encoder.i & ~event_decoder.o,
                        NextState("REPORT-EVENT")
                    ).Else(
                        NextState("WAIT-EVENT")
                    )
                )
            )
        )
        for octet_no in range(4, 0, -1):
            if octet_no == 1:
                next_state = [
                    If(event_encoder.n,
                        NextState("WAIT-EVENT")
                    ).Else(
                        NextState("REPORT-EVENT")
                    )
                ]
            else:
                next_state = [
                    NextState("REPORT-EVENT-DATA-%d" % (octet_no - 1))
                ]
            serializer.act("REPORT-EVENT-DATA-%d" % octet_no,
                If(self.output_fifo.writable,
                    self.output_fifo.din.eq(event_data.part((octet_no - 1) * 8, 8)),
                    self.output_fifo.we.eq(1),
                    *next_state
                )
            )
            serializer.act("REPORT-DONE",
                If(self.output_fifo.writable,
                    self.output_fifo.din.eq(REPORT_DONE),
                    self.output_fifo.we.eq(1),
                    NextState("DONE")
                )
            )
            serializer.act("DONE",
                If(~self.done,
                    NextState("WAIT-EVENT")
                )
            )


class TraceDecodingError(Exception):
    pass


class TraceDecoder:
    """
    Event analyzer trace decoder.

    Decodes raw analyzer traces into a timestamped sequence of maps from event fields to
    their values.
    """
    def __init__(self, event_sources, absolute_timestamps=True):
        self.event_sources       = event_sources
        self.absolute_timestamps = absolute_timestamps

        self._state      = "IDLE"
        self._byte_off   = 0
        self._timestamp  = 0
        self._delay      = 0
        self._event_src  = 0
        self._event_off  = 0
        self._event_data = 0
        self._pending    = OrderedDict()
        self._timeline   = []

    def events(self):
        """
        Return names and widths for all events that may be emitted by this trace decoder.
        """
        for event_source in self.event_sources:
            if event_source.fields:
                for field_name, field_width in event_source.fields:
                    yield ("%s-%s" % (field_name, event_source.name), field_width)
            else:
                yield (event_source.name, event_source.width)

    def process(self, data):
        """
        Incrementally parse a chunk of analyzer trace, and record events in it.
        """
        for octet in data:
            is_delay = ((octet & REPORT_DELAY_MASK) == REPORT_DELAY)
            is_event = ((octet & REPORT_EVENT_MASK) == REPORT_EVENT)
            is_done  = ((octet & REPORT_DONE_MASK)  == REPORT_DONE)

            if self._state == "IDLE" and is_delay:
                self._state = "DELAY"
                self._delay = octet & ~REPORT_DELAY_MASK
            elif self._state == "DELAY" and is_delay:
                self._delay = (self._delay << 7) | (octet & ~REPORT_DELAY_MASK)
            elif self._state in ("IDLE", "DELAY") and is_event:
                if self._delay > 0:
                    if self._pending:
                        self._timeline.append((self._timestamp, self._pending))
                        self._pending = OrderedDict()
                    if self.absolute_timestamps:
                        self._timestamp += self._delay
                    else:
                        self._timestamp  = self._delay
                    self._delay = 0

                if (octet & ~REPORT_EVENT_MASK) > len(self.event_sources):
                    raise TraceDecodingError("at byte offset %d: event source out of bounds" %
                                             self._byte_off)
                self._event_src = self.event_sources[octet & ~REPORT_EVENT_MASK]
                if self._event_src.width == 0:
                    self._pending[self._event_src.name] = None
                    self._state = "IDLE"
                else:
                    self._event_off  = self._event_src.width
                    self._event_data = 0
                    self._state = "EVENT"
            elif self._state == "EVENT":
                self._event_data <<= 8
                self._event_data  |= octet
                if self._event_off > 8:
                    self._event_off -= 8
                else:
                    if self._event_src.fields:
                        offset = 0
                        for field_name, field_width in self._event_src.fields:
                            self._pending["%s-%s" % (field_name, self._event_src.name)] = \
                                (self._event_data >> offset) & ((1 << field_width) - 1)
                            offset += field_width
                    else:
                        self._pending[self._event_src.name] = self._event_data

                    self._state = "IDLE"
            elif self._state == "IDLE" and is_done:
                self._state = "DONE"
            else:
                raise TraceDecodingError("at byte offset %d: invalid byte %#04x for state %s" %
                                         (self._byte_off, octet, self._state))

            self._byte_off += 1

    def flush(self, pending=False):
        """
        Return the complete event timeline since the start of decoding or the previous flush.
        If ``pending`` is ``True``, also flushes pending events; this may cause duplicate
        timestamps if more events arrive after the flush.
        """
        if pending and self._pending or self._state == "DONE":
            self._timeline.append((self._timestamp, self._pending))
            self._pending = OrderedDict()
        timeline, self._timeline = self._timeline, []
        return timeline

    def is_done(self):
        return self._state == "DONE"

# -------------------------------------------------------------------------------------------------

import unittest
from migen.fhdl import verilog

from . import simulation_test


class EventAnalyzerTestbench(Module):
    def __init__(self, **kwargs):
        self.submodules.fifo = SyncFIFOBuffered(width=8, depth=64)
        self.submodules.dut  = EventAnalyzer(self.fifo, **kwargs)

    def trigger(self, index, data):
        yield self.dut.event_sources[index].trigger.eq(1)
        yield self.dut.event_sources[index].data.eq(data)

    def step(self):
        yield
        for event_source in self.dut.event_sources:
            yield event_source.trigger.eq(0)

    def read(self, count, limit=128):
        data  = []
        cycle = 0
        while len(data) < count:
            while not (yield self.fifo.readable) and cycle < limit:
                yield
                cycle += 1
            if not (yield self.fifo.readable):
                raise ValueError("FIFO underflow")
            data.append((yield from self.fifo.read()))

        cycle = 16
        while not (yield self.fifo.readable) and cycle < limit:
            yield
            cycle += 1
        if (yield self.fifo.readable):
            raise ValueError("junk in FIFO: %#04x at %d" % ((yield self.fifo.dout), count))

        return data


class EventAnalyzerTestCase(unittest.TestCase):
    def setUp(self):
        self.tb = EventAnalyzerTestbench(event_depth=4)

    def configure(self, tb, sources):
        for n, args in enumerate(sources):
            if not isinstance(args, tuple):
                args = (args,)
            tb.dut.add_event_source(str(n), *args)

    def assertEmitted(self, tb, data, decoded, flush_pending=True):
        self.assertEqual((yield from tb.read(len(data))), data)

        decoder = TraceDecoder(self.tb.dut.event_sources)
        decoder.process(data)
        self.assertEqual(decoder.flush(flush_pending), decoded)

    @simulation_test(sources=(8,))
    def test_one_8bit_src(self, tb):
        yield from tb.trigger(0, 0xaa)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|2,
            REPORT_EVENT|0, 0xaa,
        ], [
            (2, {"0": 0xaa}),
        ])

    @simulation_test(sources=(8,8))
    def test_two_8bit_src(self, tb):
        yield from tb.trigger(0, 0xaa)
        yield from tb.trigger(1, 0xbb)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|2,
            REPORT_EVENT|0, 0xaa,
            REPORT_EVENT|1, 0xbb,
        ], [
            (2, {"0": 0xaa, "1": 0xbb}),
        ])

    @simulation_test(sources=(12,))
    def test_one_12bit_src(self, tb):
        yield from tb.trigger(0, 0xabc)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|2,
            REPORT_EVENT|0, 0x0a, 0xbc,
        ], [
            (2, {"0": 0xabc}),
        ])

    @simulation_test(sources=(16,))
    def test_one_16bit_src(self, tb):
        yield from tb.trigger(0, 0xabcd)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|2,
            REPORT_EVENT|0, 0xab, 0xcd,
        ], [
            (2, {"0": 0xabcd}),
        ])

    @simulation_test(sources=(24,))
    def test_one_24bit_src(self, tb):
        yield from tb.trigger(0, 0xabcdef)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|2,
            REPORT_EVENT|0, 0xab, 0xcd, 0xef
        ], [
            (2, {"0": 0xabcdef}),
        ])

    @simulation_test(sources=(32,))
    def test_one_32bit_src(self, tb):
        yield from tb.trigger(0, 0xabcdef12)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|2,
            REPORT_EVENT|0, 0xab, 0xcd, 0xef, 0x12
        ], [
            (2, {"0": 0xabcdef12}),
        ])

    @simulation_test(sources=(0,))
    def test_one_0bit_src(self, tb):
        yield from tb.trigger(0, 0)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|2,
            REPORT_EVENT|0,
        ], [
            (2, {"0": None}),
        ])

    @simulation_test(sources=(0,0))
    def test_two_0bit_src(self, tb):
        yield from tb.trigger(0, 0)
        yield from tb.trigger(1, 0)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|2,
            REPORT_EVENT|0,
            REPORT_EVENT|1,
        ], [
            (2, {"0": None, "1": None}),
        ])

    @simulation_test(sources=(0,1))
    def test_0bit_1bit_src(self, tb):
        yield from tb.trigger(0, 0)
        yield from tb.trigger(1, 1)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|2,
            REPORT_EVENT|0,
            REPORT_EVENT|1, 0b1
        ], [
            (2, {"0": None, "1": 0b1}),
        ])

    @simulation_test(sources=(1,0))
    def test_1bit_0bit_src(self, tb):
        yield from tb.trigger(0, 1)
        yield from tb.trigger(1, 0)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|2,
            REPORT_EVENT|0, 0b1,
            REPORT_EVENT|1,
        ], [
            (2, {"0": 0b1, "1": None}),
        ])

    @simulation_test(sources=((3, (("a", 1), ("b", 2))),))
    def test_fields(self, tb):
        yield from tb.trigger(0, 0b101)
        yield from tb.step()
        yield from tb.trigger(0, 0b110)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|2,
            REPORT_EVENT|0, 0b101,
            REPORT_DELAY|1,
            REPORT_EVENT|0, 0b110,
        ], [
            (2, {"a-0": 0b1, "b-0": 0b10}),
            (3, {"a-0": 0b0, "b-0": 0b11}),
        ])

    @simulation_test(sources=(8,))
    def test_delay(self, tb):
        yield
        yield
        yield from tb.trigger(0, 0xaa)
        yield from tb.step()
        yield
        yield from tb.trigger(0, 0xbb)
        yield from tb.step()
        yield
        yield
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|4,
            REPORT_EVENT|0, 0xaa,
            REPORT_DELAY|2,
            REPORT_EVENT|0, 0xbb,
        ], [
            (4, {"0": 0xaa}),
            (6, {"0": 0xbb}),
        ])

    @simulation_test(sources=(1,))
    def test_delay_2_septet(self, tb):
        yield tb.dut._delay_timer.eq(0b1_1110000)
        yield from tb.trigger(0, 1)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|0b0000001,
            REPORT_DELAY|0b1110000,
            REPORT_EVENT|0, 0b1
        ], [
            (0b1_1110000, {"0": 0b1}),
        ])

    @simulation_test(sources=(1,))
    def test_delay_3_septet(self, tb):
        yield tb.dut._delay_timer.eq(0b01_0011000_1100011)
        yield from tb.trigger(0, 1)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|0b0000001,
            REPORT_DELAY|0b0011000,
            REPORT_DELAY|0b1100011,
            REPORT_EVENT|0, 0b1
        ], [
            (0b01_0011000_1100011, {"0": 0b1}),
        ])

    @simulation_test(sources=(1,))
    def test_delay_max(self, tb):
        yield tb.dut._delay_timer.eq(0xffff)
        yield from tb.trigger(0, 1)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|0b0000011,
            REPORT_DELAY|0b1111111,
            REPORT_DELAY|0b1111111,
            REPORT_EVENT|0, 0b1
        ], [
            (0xffff, {"0": 0b1}),
        ])

    @simulation_test(sources=(1,))
    def test_delay_overflow(self, tb):
        yield tb.dut._delay_timer.eq(0xffff)
        yield
        yield from tb.trigger(0, 1)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|0b0000100,
            REPORT_DELAY|0b0000000,
            REPORT_DELAY|0b0000000,
            REPORT_EVENT|0, 0b1
        ], [
            (0x10000, {"0": 0b1}),
        ])

    @simulation_test(sources=(1,))
    def test_delay_overflow_p1(self, tb):
        yield tb.dut._delay_timer.eq(0xffff)
        yield
        yield
        yield from tb.trigger(0, 1)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|0b0000100,
            REPORT_DELAY|0b0000000,
            REPORT_DELAY|0b0000001,
            REPORT_EVENT|0, 0b1
        ], [
            (0x10001, {"0": 0b1}),
        ])

    @simulation_test(sources=(1,))
    def test_delay_4_septet(self, tb):
        for _ in range(64):
            yield tb.dut._delay_timer.eq(0xffff)
            yield

        yield from tb.trigger(0, 1)
        yield from tb.step()
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|0b0000001,
            REPORT_DELAY|0b1111111,
            REPORT_DELAY|0b1111111,
            REPORT_DELAY|0b1000001,
            REPORT_EVENT|0, 0b1
        ], [
            (0xffff * 64 + 1, {"0": 0b1}),
        ])

    @simulation_test(sources=(1,))
    def test_done(self, tb):
        yield from tb.trigger(0, 1)
        yield from tb.step()
        yield
        yield tb.dut.done.eq(1)
        yield from self.assertEmitted(tb, [
            REPORT_DELAY|2,
            REPORT_EVENT|0, 0b1,
            REPORT_DONE
        ], [
            (2, {"0": 0b1})
        ], flush_pending=False)