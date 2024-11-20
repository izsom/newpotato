import json
import logging
import traceback
from collections import Counter, defaultdict
from itertools import chain
from typing import Any, Dict, List, Optional, Tuple

import networkx as nx
from tuw_nlp.graph.graph import Graph
from tuw_nlp.graph.ud_graph import UDGraph
from tuw_nlp.graph.utils import GraphFormulaPatternMatcher
from tuw_nlp.text.utils import tuple_if_list

from newpotato.datatypes import GraphMappedTriplet, Triplet
from newpotato.extractors.extractor import Extractor
from newpotato.extractors.graph_parser_client import GraphParserClient


class GraphBasedExtractor(Extractor):
    @staticmethod
    def from_json(data: Dict[str, Any]):
        extractor = GraphBasedExtractor()
        extractor.text_parser.check_params(data["parser_params"])

        extractor.parsed_graphs = {
            tuple_if_list(item["text"]): UDGraph.from_json(item["graph"])
            for item in data["parsed_graphs"]
        }

        return extractor

    def to_json(self) -> Dict[str, Any]:
        # TODO learned rules are not yet saved
        return {
            "extractor_type": "graph",
            "parsed_graphs": [
                {"text": text, "graph": graph.to_json()}
                for text, graph in self.parsed_graphs.items()
            ],
            "parser_params": self.text_parser.get_params(),
        }

    def save(self, fn: str):
        with open(fn, "w") as f:
            f.write(json.dumps(self.to_json()))

    def _patterns_from_json(self, patterns):
        return Counter(
            {
                Graph.from_penman(pn_graph, node_attr="name|upos"): count
                for pn_graph, count in patterns.items()
            }
        )

    def _patterns_to_json(self, graphs):
        return {
            graph.to_penman(name_attr="name|upos"): count
            for graph, count in graphs.most_common()
        }

    def _triplet_patterns_from_json(self, patterns):
        return Counter(
            {
                (
                    Graph.from_penman(p["pattern"], node_attr="name|upos"),
                    tuple(p["arg_roots"]),
                    tuple(p["inferred_nodes"]),
                ): p["count"]
                for p in patterns
            }
        )

    def _triplet_patterns_to_json(self, graphs):
        return [
            {
                "pattern": pattern[0].to_penman(name_attr="name|upos"),
                "arg_roots": pattern[1],
                "inferred_nodes": pattern[2],
                "count": count,
            }
            for pattern, count in graphs.items()
        ]

    def patterns_to_json(self):
        return {
            "pred_graphs": self._patterns_to_json(self.pred_graphs),
            "all_arg_graphs": self._patterns_to_json(self.all_arg_graphs),
            "arg_graphs_by_pred": {
                " ".join(pred): self._patterns_to_json(arg_graphs)
                for pred, arg_graphs in self.arg_graphs_by_pred.items()
            },
            "triplet_graphs": self._triplet_patterns_to_json(self.triplet_graphs),
            "triplet_graphs_by_pred": {
                " ".join(pred): self._triplet_patterns_to_json(tr_graphs)
                for pred, tr_graphs in self.triplet_graphs_by_pred.items()
            },
            "patterns_to_sens": {
                graph.to_penman(name_attr="name|upos"): [text for text in sorted(texts)]
                for graph, texts in self.patterns_to_sens.items()
            },
        }

    def load_patterns(self, fn: str):
        with open(fn) as f:
            d = json.load(f)

        self.pred_graphs = self._patterns_from_json(d["pred_graphs"])
        self.all_arg_graphs = self._patterns_from_json(d["all_arg_graphs"])
        self.arg_graphs_by_pred = {
            tuple(pred.split(" ")): self._patterns_from_json(arg_patterns)
            for pred, arg_patterns in d["arg_graphs_by_pred"].items()
        }
        self.triplet_graphs = self._triplet_patterns_from_json(d["triplet_graphs"])
        self.triplet_graphs_by_pred = {
            tuple(pred.split(" ")): self._triplet_patterns_from_json(tr_patterns)
            for pred, tr_patterns in d["triplet_graphs_by_pred"].items()
        }
        self._get_matchers()
        logging.info(f"{self.pred_graphs=}")
        self.patterns_to_sens = {
            Graph.from_penman(pn_graph, node_attr="name|upos"): set(sens)
            for pn_graph, sens in d["patterns_to_sens"].items()
        }

    def save_patterns(self, fn: str):
        with open(fn, "w") as f:
            f.write(json.dumps(self.patterns_to_json(), indent=4))

    def __init__(
        self,
        parser_url: Optional[str] = "http://localhost:7277",
        default_relation: Optional[str] = None,
    ):
        super(GraphBasedExtractor, self).__init__()
        self.text_parser = GraphParserClient(parser_url)
        self.default_relation = default_relation
        self.n_rules = 0

    def _parse_sen_tuple(self, sen_tuple: Tuple):
        """
        Parse pretokenized sentence.

        Args:
            sen_tuple (Tuple): The pretokenized sentence.

        Returns:
            TODO
        """
        graph = self.text_parser.parse_pretokenized(sen_tuple)
        return sen_tuple, graph

    def _parse_text(self, text: str):
        """
        Parse the given text.

        Args:
            text (str): The text to parse.

        Returns:
            TODO
        """
        graphs = self.text_parser.parse(text)
        for graph in graphs:
            yield graph.text, graph

    def get_tokens(self, sen) -> List[str]:
        """
        Get the tokens of the given text.
        """
        return self.parsed_graphs[sen].tokens

    def get_lemmas(self, sen) -> List[str]:
        """
        Get the lemmas of the given text.
        """
        return [w.lemma for w in self.parsed_graphs[sen].stanza_sen.words]

    def _get_patterns(self, text_to_triplets):
        patterns_to_sens = defaultdict(set)
        pred_graphs = Counter()
        triplet_graphs = Counter()
        triplet_graphs_by_pred = defaultdict(Counter)
        arg_graphs_by_pred = defaultdict(Counter)
        all_arg_graphs = Counter()
        logging.info(f"Get patterns: {text_to_triplets=}")
        # a dict the key is the sentence and the value is a list of triplets in the sentence and a boolean value
        for text, triplets in text_to_triplets.items():
            # toks = self.get_tokens(text)
            logging.debug(f"{text=}")
            graph = self.parsed_graphs[text]
            logging.debug(graph.to_dot())
            lemmas = self.get_lemmas(text) # get the lemmas of the words in the sentence
            for triplet, positive in triplets:
                logging.debug(f"{triplet=}") # =<class 'newpotato.datatypes.GraphMappedTriplet'>
                if triplet.pred is not None:
                    logging.debug(f"{triplet.pred_graph=}")
                    pred_graphs[triplet.pred_graph] += 1
                    patterns_to_sens[triplet.pred_graph].add(text)
                    pred_lemmas = tuple(lemmas[i] for i in triplet.pred) # get the lemmas of the predicate
                    # triplet_toks = set(chain(triplet.pred, triplet.arg_roots))
                    triplet_toks = set(chain(triplet.pred, *triplet.args))
                else:
                    pred_lemmas = (self.default_relation,)
                    # triplet_toks = set(triplet.arg_roots)
                    triplet_toks = set(chain(*triplet.args))

                for arg_graph in triplet.arg_graphs:
                    logging.debug(f"{arg_graph=}")
                    arg_graphs_by_pred[pred_lemmas][arg_graph] += 1
                    all_arg_graphs[arg_graph] += 1
                    patterns_to_sens[arg_graph].add(text)

                logging.debug(f"{triplet_toks=}")

                triplet_graph = graph.subgraph(
                    triplet_toks, handle_unconnected="shortest_path"
                )

                # the list of arg roots is stored to map nodes to arguments
                # inferred nodes are stored so they can be ignored at matching time
                # both are stored by lextop indices
                pattern_key = (
                    triplet_graph,
                    tuple(triplet_graph.index_nodes(triplet.arg_roots)),
                    tuple(triplet_graph.index_inferred_nodes()),
                )
                logging.info(f"Triplet_graphs Counter gets this value: {pattern_key=}")
                triplet_graphs[pattern_key] += 1
                triplet_graphs_by_pred[pred_lemmas][pattern_key] += 1
                patterns_to_sens[triplet_graph].add(text)
                logging.debug(f"{triplet_graph=}")

                if triplet.pred is None:
                    inferred_pred_toks = set(
                        node
                        for node in triplet_graph.G.nodes()
                        if node not in triplet_toks
                    )
                    logging.debug(f"{inferred_pred_toks=}")
                    inferred_pred_graph = graph.subgraph(
                        inferred_pred_toks, handle_unconnected="shortest_path"
                    )
                    logging.debug(f"{inferred_pred_graph=}")
                    pred_graphs[inferred_pred_graph] += 1

        self.pred_graphs = pred_graphs
        
        self.all_arg_graphs = all_arg_graphs
        self.arg_graphs_by_pred = arg_graphs_by_pred
        self.triplet_graphs = triplet_graphs
        self.triplet_graphs_by_pred = triplet_graphs_by_pred
        self.patterns_to_sens = patterns_to_sens

    def _get_matcher_from_graphs(self, graphs, label, threshold):
        patterns = []
        for graph, freq in graphs.most_common():
            if freq < threshold:
                break
            if isinstance(graph, Graph):
                patterns.append(((graph.G,), (), label))
            else:
                patterns.append(((graph[0].G,), (), label))
            logging.info(f"Matcher from graph: {graph.to_penman()=}")
        logging.info(f"Matcher from graph: {patterns=}")

        matcher = GraphFormulaPatternMatcher(
            patterns, converter=None, case_sensitive=False
        )
        logging.info(f"Matcher from graph: {matcher=}")
        return matcher

    def _get_triplet_matchers(self):
        logging.info(f"{self.triplet_graphs=}") # is a counter object, with the value of pattern_key as key and the frequency as value
        return Counter(
            {
                (
                    self._get_matcher_from_graphs(
                        Counter({graph: count}), label="TRI", threshold=1
                    ),
                    arg_root_indices,
                    inferred_node_indices,
                    graph,
                ): count # {(keys) : value}
                for (
                    graph,
                    arg_root_indices,
                    inferred_node_indices,
                ), count in self.triplet_graphs.most_common()
            }
        )

    def _get_triplet_matchers_by_pred(self):
        return {
            pred_lemmas: Counter(
                {
                    (
                        self._get_matcher_from_graphs(
                            Counter({graph: count}), label="TRI", threshold=1
                        ),
                        arg_root_indices,
                        inferred_node_indices,
                        graph,
                    ): count
                    for (
                        graph,
                        arg_root_indices,
                        inferred_node_indices,
                    ), count in triplet_graph_counter.most_common()
                }
            )
            for pred_lemmas, triplet_graph_counter in self.triplet_graphs_by_pred.items()
        }

    def _get_matchers(self):
        self.pred_matcher = self._get_matcher_from_graphs(
            self.pred_graphs, label="PRED", threshold=1
        )
        self.arg_matcher = self._get_matcher_from_graphs(
            self.all_arg_graphs, label="ARG", threshold=1
        )
        self.triplet_matchers = self._get_triplet_matchers() # returns a counter object
        self.triplet_matchers_by_pred = self._get_triplet_matchers_by_pred() # returns a dictionary of counter objects with pred lemma as key
        self.n_rules = len(self.pred_matcher.patts)

    def get_rules(self, text_to_triplets, **kwargs):
        logging.info("collecting patterns...")
        self._get_patterns(text_to_triplets)
        logging.info("getting rules...")
        self._get_matchers()

        self._is_trained = True
        return [graph for (graph, _, __), ___ in self.triplet_graphs.most_common(20)]

    def print_rules(self, console):
        console.print("[bold green]Extracted Rules:[/bold green]")
        console.print(f"{self.pred_graphs.most_common(50)=}")
        console.print(f"{self.all_arg_graphs.most_common(50)=}")
        console.print(f"{self.triplet_graphs.most_common(50)=}")
        logging.info(f"Writing rules to file...")
        for udg in self.triplet_graphs:
            # console.print(f"{udg}")
            f = open("C:/Users/zsomb/OneDrive - TU Wien/egyetem/TU WIEN/DataScience/MasterThesis/code/F_voetutor/VOE-tutor/PoolOfRules/graphsOUT.txt", "a")
            f.write(f"Annotation is:{udg[0].text}\n")
            f.write(f"Penman: {UDGraph.to_penman(udg[0])}\n")
            f.write(f"Dot: {UDGraph.to_dot(udg[0])}\n")
            f.write("--------------------\n")
            f.close()            
            # console.print(f"{UDGraph.to_penman(udg[0])}")
        logging.info(f"Writing rules to file done")

    def get_n_rules(self):
        return self.n_rules

    def extract_triplets_from_text(self, text, **kwargs):
        matches_by_text = {}
        for sen, triplets_and_subgraphs in self._infer_triplets(text):
            logging.info(f"Triplets from text: {sen=}, {triplets_and_subgraphs=}")
            if sen not in matches_by_text.keys():
                matches_by_text[sen] = {
                    "matches": [],
                    "rules_triggered": [],
                    "triplets": [],
                }
            logging.info(f"Extract triplets from text | found triplets: {triplets_and_subgraphs=}")
            # for triplet, subgraph in triplets_and_subgraphs: # TypeError: 'GraphMappedTriplet' object is not iterable
            #     matches_by_text[sen]["rules_triggered"].append(subgraph)
            #     matches_by_text[sen]["triplets"].append(triplet)
            #     matches_by_text[sen]["matches"].append(
            #         {"REL": None, "ARG0": None, "ARG1": None}
            #     )
            
            matches_by_text[sen]["rules_triggered"].append(triplets_and_subgraphs)
            matches_by_text[sen]["triplets"].append(triplets_and_subgraphs)
            matches_by_text[sen]["matches"].append(
                {"REL": None, "ARG0": None, "ARG1": None}
            )
            
        logging.info(f"Extract triplets from text | {matches_by_text=}")
        return matches_by_text

    def map_triplet(self, triplet, sentence, **kwargs):
        graph = self.parsed_graphs[sentence]
        logging.debug(f"mapping triplet to {graph=}, when sentence is {sentence=}")
        logging.debug(f"The dot of the graph: {graph.to_dot()=}")
        pred_subgraph = (
            graph.subgraph(triplet.pred, handle_unconnected="shortest_path") # defined in UDGraph
            if triplet.pred is not None
            else None
        ) # a fully connected version of the subgraph described by triplet.pred

        logging.debug(f"triplet mapped: {pred_subgraph=}")

        arg_subgraphs = [
            graph.subgraph(arg, handle_unconnected="shortest_path")
            if arg is not None and len(arg) > 0
            else None
            for arg in triplet.args
        ]
        logging.debug(f"triplet mapped: {arg_subgraphs=}")
        return GraphMappedTriplet(triplet, pred_subgraph, arg_subgraphs)

    def _match(self, matcher, sen_graph, attrs):
        # get the subgraphs that match the given matcher
        logging.info(f"_match: the sen_graph for the matcher.match(): {sen_graph.tokens=}")
        for key, i, subgraphs in matcher.match(
            sen_graph.G, return_subgraphs=True, attrs=attrs
        ):
            for subgraph in subgraphs:
                # extract the subgraph from sen_graph, that is consisted of the nodes of the 'subgraph'
                ud_subgraph = sen_graph.subgraph(subgraph.nodes)
                indices = frozenset(
                    idx
                    for idx, token in enumerate(ud_subgraph.tokens)
                    if token is not None
                ) # indices of the tokens in the subgraph
                logging.debug(f"MATCH: {indices=}, {ud_subgraph=}")
                yield indices, ud_subgraph

    def _get_arg_cands(self, sen_graph):
        roots_to_cands_by_indices = defaultdict(dict)
        for indices, subgraph in self._match(
            self.arg_matcher, sen_graph, attrs=("upos",) # matches nodes based on the 'upos' attribute
        ):
            roots_to_cands_by_indices[subgraph.root][indices] = subgraph # root node as key

        arg_roots_to_arg_cands = {}
        for root, cands in roots_to_cands_by_indices.items(): # item is the key-value pair, root being the key and cands being the items
            logging.info(f"{root=}, {cands=}") # cands={frozenset({10}): UDGraph(10_Schaden)}
            largest = max(cands.keys(), key=len) # the key with the largest length of frozen set
            arg_roots_to_arg_cands[root] = (largest, cands[largest])

        return arg_roots_to_arg_cands

    def _gen_raw_triplets(
        self,
        sen,
        sen_graph, # is an instance of UDGraph
        pred_cands, # e.g: {frozenset({21}): UDGraph(22_Angelegenheit)}
        arg_roots_to_arg_cands, # e.g: arg_roots_to_arg_cands={2: (frozenset({2}), UDGraph(2_Gustav)), 4: (frozenset({4}), UDGraph(4_Bernds)), ...}
        include_partial,
        triplet_matchers=None,
    ):
        if triplet_matchers is None:
            triplet_matchers = self.triplet_matchers # it is a counter object of a dictionary, described below
        logging.info(f"{triplet_matchers=}")
        for (
            triplet_matcher, # a GraphFormulaPatternMatcher object
            arg_root_indices, # a tuple of indices
            inferred_node_indices, # a tuple of indices
            patt_graph, # a Graph object
        ), freq in triplet_matchers.most_common():
            logging.debug(f"Triplet matcher loop _gen_raw_trip: {triplet_matcher=}")
            logging.debug(f"{arg_root_indices=}")
            logging.debug(f"{inferred_node_indices=}")
            logging.debug(f"{patt_graph=}")
            logging.debug(f"_gen_raw_triplets : {sen_graph=}")
            triplet_cands = set(
                indices
                for indices in self._match(triplet_matcher, sen_graph, attrs=("upos",))
            ) # is the indices of the tokens in the sentence that match the triplet_matcher
            logging.debug(f"{triplet_cands=}") # triplet_cands={(frozenset({16, 17, 14, 15}), UDGraph(15_handeln 14_Bernd 17_fahrlässig 16_grob))}
            logging.debug(f"{inferred_node_indices=}")
            for triplet_cand, triplet_graph in triplet_cands:
                inferred_nodes = set(
                    triplet_graph.nodes_by_lextop(inferred_node_indices) # defined in graph.py, part of class Graph
                ) # returnes the nodes in a topological order, meaning source nodes precede the target nodes in a targeted graph
                # inferred_nodes: the indices of the nodes of the triplet_graph that are on the node positions describedd by inferred_node_indices
                arg_roots = triplet_graph.nodes_by_lextop(arg_root_indices)
                logging.debug("==========================")
                logging.debug(f"{triplet_cand=}")
                logging.debug(f"{triplet_graph=}")
                logging.debug(f"{inferred_nodes=}")
                logging.debug(f"{arg_roots=}")
                logging.debug(f"{pred_cands=}")
                for pred_cand in pred_cands:
                    if not pred_cand.issubset(triplet_cand):
                        continue  # skip if pred_cand is not in the triplet
                    # from now on pred_cand is part of the triplet_cand
                    covered_args = triplet_cand - pred_cand - inferred_nodes
                    logging.info(f"{pred_cand=}")
                    logging.info(f"{covered_args=}")
                    args = [
                        sorted(arg_roots_to_arg_cands[arg_root][0]) # get the indices of potential arguments
                        if arg_root in covered_args
                        else None
                        for arg_root in arg_roots
                    ]
                    logging.info(f"{args=}") # good example to understand: args=[[0], [5, 6]]
                    # if any arg is None, the triplet is partial
                    partial = any(arg is None for arg in args) # boolean
                    if partial and not include_partial: # when would it be partial? when is the root index None
                        continue
                    triplet = Triplet(pred_cand, args, toks=sen_graph.tokens) # stores the integer indices of pred and args as integers
                    try:
                        mapped_triplet = self.map_triplet(triplet, sen) # returns a GraphMappedTriplet(triplet, pred_subgraph, arg_subgraphs)
                        # 
                        logging.info(f"inferring this triplet: {triplet}")
                        logging.info(
                            f"based on this pattern: {patt_graph.to_penman(name_attr='name|upos')}"
                        )
                        logging.info(
                            f"sentences with this pattern: {self.patterns_to_sens[patt_graph]}"
                        )
                        logging.info(f"mapped triplet: {mapped_triplet}")
                        logging.info(f"mapped triplet type : {type(mapped_triplet)}")
                        logging.info(f"sentence: {sen}")
                        yield sen, mapped_triplet
                    except (
                        KeyError,
                        nx.exception.NetworkXPointlessConcept,
                    ):
                        logging.error(f"error mapping triplet: {triplet=}, {sen=}")
                        logging.error(traceback.format_exc())
                        logging.error("skipping")

    def _gen_raw_triplets_lexical(
        self, sen, sen_graph, pred_cands, arg_roots_to_arg_cands, include_partial
    ):
        for pred_cand in pred_cands:
            logging.debug(f"{pred_cand=}")
            pred_lemmas = tuple(sen_graph.G.nodes[i]["name"] for i in pred_cand)
            logging.debug(f"{pred_lemmas=}")
            if pred_lemmas not in self.triplet_matchers_by_pred:
                logging.debug("unknown pred lemmas, skipping")
                continue
            triplet_matchers = self.triplet_matchers_by_pred[pred_lemmas]

            yield from self._gen_raw_triplets(
                sen,
                sen_graph,
                (pred_cand,),
                arg_roots_to_arg_cands,
                include_partial=include_partial,
                triplet_matchers=triplet_matchers,
            )

    def gen_raw_triplets(
        self,
        sen,
        sen_graph,
        pred_cands,
        arg_roots_to_arg_cands,
        lexical,
        include_partial,
    ):
        if lexical:
            yield from self._gen_raw_triplets_lexical(
                sen, sen_graph, pred_cands, arg_roots_to_arg_cands
            )
        else:
            yield from self._gen_raw_triplets(
                sen,
                sen_graph,
                pred_cands,
                arg_roots_to_arg_cands,
                include_partial=include_partial,
            )

    def _infer_triplets(self, text: str, lexical=False, include_partial=False):
        for sen, sen_graph in self.parse_text(text):
            logging.debug("==========================")
            logging.debug("==========================")
            logging.debug(f"{sen_graph=}")
            logging.info(f"{sen_graph.tokens=}")
            logging.info(f"{sen_graph.G.nodes=}")
            logging.info(f"{sen_graph.to_dot()=}")
            # logging.info(f"{nx.to_latex(sen_graph.G, node_label='text')=}")
            # sen_graph is an instance of UDGraph of the given sentence
            pred_cands = {
                indices: subgraph
                for indices, subgraph in self._match(
                    self.pred_matcher, sen_graph, attrs=None
                ) # that match the predicate rules
            } # an index of the tokens in the sentence and the Ugraph of that sentence
            # get the root number and the corresponding largest UDGraph candidate
            arg_roots_to_arg_cands = self._get_arg_cands(sen_graph)
            
            logging.info(f"{arg_roots_to_arg_cands=}") # arg_roots_to_arg_cands={2: (frozenset({2}), UDGraph(2_Gustav)), 4: (frozenset({4}), UDGraph(4_Bernds)), ...
            logging.info(f"{pred_cands=}") # {frozenset({21}): UDGraph(22_Angelegenheit)}
            yield from self.gen_raw_triplets(
                sen,
                sen_graph,
                pred_cands,
                arg_roots_to_arg_cands,
                lexical=lexical,
                include_partial=include_partial,
            )

    def infer_triplets(self, text: str, **kwargs) -> List[Triplet]:
        triplets = sorted(set(triplet for sen, triplet in self._infer_triplets(text)))
        logging.info(f"ZSOM --- {triplets=} for {text=}")
        return triplets


if __name__ == "__main__":
    # for testing
    logging.basicConfig(
        # level=logging.DEBUG,
        level=logging.WARNING,
        format="%(asctime)s : %(module)s (%(lineno)s) - %(levelname)s - %(message)s",
        force=True,
    )
    import sys

    extractor = GraphBasedExtractor()
    extractor.load_patterns(sys.argv[1])
    text = sys.stdin.read().strip()
    for sen, triplet in extractor._infer_triplets(text):
        print(f"{sen}\t{triplet}")
