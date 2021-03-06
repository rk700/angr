
import logging

from collections import defaultdict
from sortedcontainers import  SortedDict

from archinfo.arch_soot import SootMethodDescriptor, SootAddressDescriptor

from .. import register_analysis
from ...errors import AngrCFGError, SimMemoryError, SimEngineError
from ...codenode import HookNode, SootBlockNode
from .cfg_fast import CFGFast, CFGJob, PendingJobs, FunctionTransitionEdge
from .cfg_node import CFGNode

l = logging.getLogger(name=__name__)

try:
    from pysoot.sootir.soot_statement import IfStmt, InvokeStmt, GotoStmt, AssignStmt
    from pysoot.sootir.soot_expr import SootInterfaceInvokeExpr, SootSpecialInvokeExpr, SootStaticInvokeExpr, \
        SootVirtualInvokeExpr, SootInvokeExpr, SootDynamicInvokeExpr
    PYSOOT_INSTALLED = True
except ImportError:
    PYSOOT_INSTALLED = False


class CFGFastSoot(CFGFast):

    def __init__(self, **kwargs):

        if not PYSOOT_INSTALLED:
            raise ImportError("Please install PySoot before analyzing Java byte code.")

        if self.project.arch.name != 'Soot':
            raise AngrCFGError('CFGFastSoot only supports analyzing Soot programs.')

        self._soot_class_hierarchy = self.project.analyses.SootClassHierarchy()
        super(CFGFastSoot, self).__init__(regions=SortedDict({}), **kwargs)

    def _pre_analysis(self):

        # Call _initialize_cfg() before self.functions is used.
        self._initialize_cfg()

        # Initialize variables used during analysis
        self._pending_jobs = PendingJobs(self.functions, self._deregister_analysis_job)
        self._traced_addresses = set()
        self._changed_functions = set()
        self._updated_nonreturning_functions = set()

        self._nodes = {}
        self._nodes_by_addr = defaultdict(list)

        self._function_returns = defaultdict(set)

        entry = self.project.entry  # type:SootAddressDescriptor
        entry_func = entry.method

        obj = self.project.loader.main_object

        if entry_func is not None:
            method_inst = obj.get_soot_method(
                entry_func.name, class_name=entry_func.class_name, params=entry_func.params)
        else:
            l.warning('The entry method is unknown. Try to find a main method.')
            method_inst = next(obj.main_methods, None)
            if method_inst is not None:
                entry_func = SootMethodDescriptor(method_inst.class_name, method_inst.name, method_inst.params)
            else:
                l.warning('Cannot find any main methods. Start from the first method of the first class.')
                for cls in obj.classes.values():
                    method_inst = next(iter(cls.methods), None)
                    if method_inst is not None:
                        break
                if method_inst is not None:
                    entry_func = SootMethodDescriptor(method_inst.class_name, method_inst.name,
                                                      method_inst.params)
                else:
                    raise AngrCFGError('There is no method in the Jar file.')

        # project.entry is a method
        # we should get the first block
        if method_inst.blocks:
            block_idx = method_inst.blocks[0].idx
            self._insert_job(CFGJob(SootAddressDescriptor(entry_func, block_idx, 0), entry_func, 'Ijk_Boring'))

        total_methods = 0

        # add all other methods as well
        for cls in self.project.loader.main_object.classes.values():
            for method in cls.methods:
                total_methods += 1
                if method.blocks:
                    method_des = SootMethodDescriptor(cls.name, method.name, method.params)
                    # TODO shouldn't this be idx?
                    block_idx = method.blocks[0].label
                    self._insert_job(CFGJob(SootAddressDescriptor(method_des, block_idx, 0), method_des, 'Ijk_Boring'))

        self._total_methods = total_methods

    def _pre_job_handling(self, job):

        if self._show_progressbar or self._progress_callback:
            if self._total_methods:
                percentage = len(self.functions) * 100.0 / self._total_methods
                self._update_progress(percentage)

    def normalize(self):
        # The Shimple CFG is already normalized.
        pass

    def _pop_pending_job(self, returning=True):

        # We are assuming all functions must return
        return self._pending_jobs.pop_job(returning=True)

    def _generate_cfgnode(self, cfg_job, current_function_addr):
        addr = cfg_job.addr

        try:

            if addr in self._nodes:
                cfg_node = self._nodes[addr]
                soot_block = cfg_node.soot_block
            else:
                soot_block = self.project.factory.block(addr).soot

                soot_block_size = self._soot_block_size(soot_block, addr.stmt_idx)

                cfg_node = CFGNode(addr, soot_block_size, self,
                                   function_address=current_function_addr, block_id=addr,
                                   soot_block=soot_block
                                   )
            return addr, current_function_addr, cfg_node, soot_block

        except (SimMemoryError, SimEngineError):
            return None, None, None, None

    def _block_get_successors(self, addr, function_addr, block, cfg_node):

        if block is None:
            # this block is not included in the artifacts...
            return [ ]

        return self._soot_get_successors(addr, function_addr, block, cfg_node)

    def _soot_get_successors(self, addr, function_id, block, cfg_node):

        # soot method
        method = self.project.loader.main_object.get_soot_method(function_id)

        block_id = block.idx

        if addr.stmt_idx is None:
            addr = SootAddressDescriptor(addr.method, block_id, 0)

        successors = [ ]

        has_default_exit = True

        next_stmt_id = block.label + len(block.statements)
        last_stmt_id = method.blocks[-1].label + len(method.blocks[-1].statements) - 1

        if next_stmt_id >= last_stmt_id:
            # there should not be a default exit going to the next block
            has_default_exit = False

        # scan through block statements, looking for those that generate new exits
        for stmt in block.statements[addr.stmt_idx - block.label : ]:
            if isinstance(stmt, IfStmt):
                succ = (stmt.label, addr,
                        SootAddressDescriptor(function_id, method.block_by_label[stmt.target].idx, stmt.target),
                        'Ijk_Boring'
                        )
                successors.append(succ)

            elif isinstance(stmt, InvokeStmt):
                invoke_expr = stmt.invoke_expr

                succs = self._soot_create_invoke_successors(stmt, addr, invoke_expr)
                if succs:
                    successors.extend(succs)
                    has_default_exit = False
                    break

            elif isinstance(stmt, GotoStmt):
                target = stmt.target
                succ = (stmt.label, addr, SootAddressDescriptor(function_id, method.block_by_label[target].idx, target),
                        'Ijk_Boring')
                successors.append(succ)

                # blocks ending with a GoTo should not have a default exit
                has_default_exit = False
                break

            elif isinstance(stmt, AssignStmt):

                expr = stmt.right_op

                if isinstance(expr, SootInvokeExpr):
                    succs = self._soot_create_invoke_successors(stmt, addr, expr)
                    if succs:
                        successors.extend(succs)
                        has_default_exit = False
                        break


        if has_default_exit:
            successors.append(('default', addr,
                               SootAddressDescriptor(function_id, method.block_by_label[next_stmt_id].idx, next_stmt_id),
                               'Ijk_Boring'
                               )
                              )

        return successors

    def _soot_create_invoke_successors(self, stmt, addr, invoke_expr):

        method_class = invoke_expr.class_name
        method_name = invoke_expr.method_name
        method_params = invoke_expr.method_params
        method_desc = SootMethodDescriptor(method_class, method_name, method_params)

        callee_soot_method = self.project.loader.main_object.get_soot_method(method_desc, none_if_missing=True)
        caller_soot_method = self.project.loader.main_object.get_soot_method(addr.method)

        if callee_soot_method is None:
            # this means the called method is external
            return [(stmt.label, addr, SootAddressDescriptor(method_desc, 0, 0), 'Ijk_Call')]

        targets = self._soot_class_hierarchy.resolve_invoke(invoke_expr, callee_soot_method, caller_soot_method)

        successors = []
        for target in targets:
            target_desc = SootMethodDescriptor(target.class_name, target.name, target.params)
            successors.append((stmt.label, addr, SootAddressDescriptor(target_desc, 0, 0), 'Ijk_Call'))

        return successors

    def _loc_to_funcloc(self, location):

        if isinstance(location, SootAddressDescriptor):
            return location.method
        return location

    def _get_plt_stubs(self, functions):

        return set()

    def _to_snippet(self, cfg_node=None, addr=None, size=None, thumb=False, jumpkind=None, base_state=None):

        assert thumb is False

        if cfg_node is not None:
            addr = cfg_node.addr
            stmts_count = cfg_node.size
        else:
            addr = addr
            stmts_count = size

        if addr is None:
            raise ValueError('_to_snippet(): Either cfg_node or addr must be provided.')

        if self.project.is_hooked(addr) and jumpkind != 'Ijk_NoHook':
            hooker = self.project._sim_procedures[addr]
            size = hooker.kwargs.get('length', 0)
            return HookNode(addr, size, type(hooker))

        if cfg_node is not None:
            soot_block = cfg_node.soot_block
        else:
            soot_block = self.project.factory.block(addr).soot

        if soot_block is not None:
            stmts = soot_block.statements
            if stmts_count is None:
                stmts_count = self._soot_block_size(soot_block, addr.stmt_idx)
            stmts = stmts[addr.stmt_idx - soot_block.label : addr.stmt_idx - soot_block.label + stmts_count]
        else:
            stmts = None
            stmts_count = 0

        return SootBlockNode(addr, stmts_count, stmts)

    def _soot_block_size(self, soot_block, start_stmt_idx):

        if soot_block is None:
            return 0

        stmts_count = 0

        for stmt in soot_block.statements[start_stmt_idx - soot_block.label : ]:
            stmts_count += 1
            if isinstance(stmt, (InvokeStmt, GotoStmt)):
                break
            if isinstance(stmt, AssignStmt) and isinstance(stmt.right_op, SootInvokeExpr):
                break

        return stmts_count

    def _scan_block(self, cfg_job):
        """
        Scan a basic block starting at a specific address

        :param CFGJob cfg_job: The CFGJob instance.
        :return: a list of successors
        :rtype: list
        """

        addr = cfg_job.addr
        current_func_addr = cfg_job.func_addr

        if self._addr_hooked_or_syscall(addr):
            entries = self._scan_procedure(cfg_job, current_func_addr)

        else:
            entries = self._scan_soot_block(cfg_job, current_func_addr)

        return entries

    def _scan_soot_block(self, cfg_job, current_func_addr):
        """
        Generate a list of successors (generating them each as entries) to IRSB.
        Updates previous CFG nodes with edges.

        :param CFGJob cfg_job: The CFGJob instance.
        :param int current_func_addr: Address of the current function
        :return: a list of successors
        :rtype: list
        """

        addr, function_addr, cfg_node, soot_block = self._generate_cfgnode(cfg_job, current_func_addr)

        # Add edges going to this node in function graphs
        cfg_job.apply_function_edges(self, clear=True)

        # function_addr and current_function_addr can be different. e.g. when tracing an optimized tail-call that jumps
        # into another function that has been identified before.

        if cfg_node is None:
            # exceptions occurred, or we cannot get a CFGNode for other reasons
            return [ ]

        self._graph_add_edge(cfg_node, cfg_job.src_node, cfg_job.jumpkind, cfg_job.src_ins_addr,
                             cfg_job.src_stmt_idx
                             )
        self._function_add_node(cfg_node, function_addr)

        # If we have traced it before, don't trace it anymore
        real_addr = self._real_address(self.project.arch, addr)
        if real_addr in self._traced_addresses:
            # the address has been traced before
            return [ ]
        else:
            # Mark the address as traced
            self._traced_addresses.add(real_addr)

        # soot_block is only used once per CFGNode. We should be able to clean up the CFGNode here in order to save memory
        cfg_node.soot_block = None

        successors = self._soot_get_successors(addr, current_func_addr, soot_block, cfg_node)

        entries = [ ]

        for suc in successors:
            stmt_idx, stmt_addr, target, jumpkind = suc

            entries += self._create_jobs(target, jumpkind, function_addr, soot_block, addr, cfg_node, stmt_addr,
                                         stmt_idx
                                         )

        return entries

    def _create_jobs(self, target, jumpkind, current_function_addr, soot_block, addr, cfg_node, stmt_addr, stmt_idx):

        """
        Given a node and details of a successor, makes a list of CFGJobs
        and if it is a call or exit marks it appropriately so in the CFG

        :param int target:          Destination of the resultant job
        :param str jumpkind:        The jumpkind of the edge going to this node
        :param int current_function_addr: Address of the current function
        :param pyvex.IRSB irsb:     IRSB of the predecessor node
        :param int addr:            The predecessor address
        :param CFGNode cfg_node:    The CFGNode of the predecessor node
        :param int ins_addr:        Address of the source instruction.
        :param int stmt_addr:       ID of the source statement.
        :return:                    a list of CFGJobs
        :rtype:                     list
        """

        target_addr = target

        jobs = [ ]

        if target_addr is None:
            # The target address is not a concrete value

            if jumpkind == "Ijk_Ret":
                # This block ends with a return instruction.
                if current_function_addr != -1:
                    self._function_exits[current_function_addr].add(addr)
                    self._function_add_return_site(addr, current_function_addr)
                    self.functions[current_function_addr].returning = True
                    self._add_returning_function(current_function_addr)

                cfg_node.has_return = True

        elif target_addr is not None:
            # This is a direct jump with a concrete target.

            # pylint: disable=too-many-nested-blocks
            if jumpkind in ('Ijk_Boring', 'Ijk_InvalICache'):
                # it might be a jumpout
                target_func_addr = None
                real_target_addr = self._real_address(self.project.arch, target_addr)
                if real_target_addr in self._traced_addresses:
                    node = self.get_any_node(target_addr)
                    if node is not None:
                        target_func_addr = node.function_address
                if target_func_addr is None:
                    target_func_addr = current_function_addr

                to_outside = not target_func_addr == current_function_addr

                edge = FunctionTransitionEdge(cfg_node, target_addr, current_function_addr,
                                              to_outside=to_outside,
                                              dst_func_addr=target_func_addr,
                                              ins_addr=stmt_addr,
                                              stmt_idx=stmt_idx,
                                              )

                ce = CFGJob(target_addr, target_func_addr, jumpkind, last_addr=addr, src_node=cfg_node,
                            src_ins_addr=stmt_addr, src_stmt_idx=stmt_idx, func_edges=[ edge ])
                jobs.append(ce)

            elif jumpkind == 'Ijk_Call' or jumpkind.startswith("Ijk_Sys"):
                jobs += self._create_job_call(addr, soot_block, cfg_node, stmt_idx, stmt_addr, current_function_addr,
                                              target_addr, jumpkind, is_syscall=False
                                              )
                self._add_returning_function(target.method)

            else:
                # TODO: Support more jumpkinds
                l.debug("Unsupported jumpkind %s", jumpkind)

        return jobs

    def make_functions(self):
        """
        Revisit the entire control flow graph, create Function instances accordingly, and correctly put blocks into
        each function.

        Although Function objects are crated during the CFG recovery, they are neither sound nor accurate. With a
        pre-constructed CFG, this method rebuilds all functions bearing the following rules:

            - A block may only belong to one function.
            - Small functions lying inside the startpoint and the endpoint of another function will be merged with the
              other function
            - Tail call optimizations are detected.
            - PLT stubs are aligned by 16.

        :return: None
        """

        tmp_functions = self.kb.functions.copy()

        for function in tmp_functions.values():
            function.mark_nonreturning_calls_endpoints()

        # Clear old functions dict
        self.kb.functions.clear()

        blockaddr_to_function = { }
        traversed_cfg_nodes = set()

        function_nodes = set()

        # Find nodes for beginnings of all functions
        for _, dst, data in self.graph.edges(data=True):
            jumpkind = data.get('jumpkind', "")
            if jumpkind == 'Ijk_Call' or jumpkind.startswith('Ijk_Sys'):
                function_nodes.add(dst)

        entry_node = self.get_any_node(self._binary.entry)
        if entry_node is not None:
            function_nodes.add(entry_node)

        for n in self.graph.nodes():
            funcloc = self._loc_to_funcloc(n.addr)
            if funcloc in tmp_functions:
                function_nodes.add(n)

        # traverse the graph starting from each node, not following call edges
        # it's important that we traverse all functions in order so that we have a greater chance to come across
        # rational functions before its irrational counterparts (e.g. due to failed jump table resolution)

        min_stage_2_progress = 50.0
        max_stage_2_progress = 90.0
        nodes_count = len(function_nodes)
        for i, fn in enumerate(function_nodes):

            if self._show_progressbar or self._progress_callback:
                progress = min_stage_2_progress + (max_stage_2_progress - min_stage_2_progress) * (i * 1.0 / nodes_count)
                self._update_progress(progress)

            self._graph_bfs_custom(self.graph, [ fn ], self._graph_traversal_handler, blockaddr_to_function,
                                   tmp_functions, traversed_cfg_nodes
                                   )

        # Don't forget those small function chunks that are not called by anything.
        # There might be references to them from data, or simply references that we cannot find via static analysis

        secondary_function_nodes = set()
        # add all function chunks ("functions" that are not called from anywhere)
        for func_addr in tmp_functions:
            node = self.get_any_node(func_addr)
            if node is None:
                continue
            if node.addr not in blockaddr_to_function:
                secondary_function_nodes.add(node)

        missing_cfg_nodes = set(self.graph.nodes()) - traversed_cfg_nodes
        missing_cfg_nodes = { node for node in missing_cfg_nodes if node.function_address is not None }
        if missing_cfg_nodes:
            l.debug('%d CFGNodes are missing in the first traversal.', len(missing_cfg_nodes))
            secondary_function_nodes |=  missing_cfg_nodes

        min_stage_3_progress = 90.0
        max_stage_3_progress = 99.9

        nodes_count = len(secondary_function_nodes)
        for i, fn in enumerate(secondary_function_nodes):

            if self._show_progressbar or self._progress_callback:
                progress = min_stage_3_progress + (max_stage_3_progress - min_stage_3_progress) * (i * 1.0 / nodes_count)
                self._update_progress(progress)

            self._graph_bfs_custom(self.graph, [fn], self._graph_traversal_handler, blockaddr_to_function,
                                   tmp_functions
                                   )

        to_remove = set()

        # remove empty functions
        for function in self.kb.functions.values():
            if function.startpoint is None:
                to_remove.add(function.addr)

        for addr in to_remove:
            del self.kb.functions[addr]

        # Update CFGNode.function_address
        for node in self._nodes.values():
            if node.addr in blockaddr_to_function:
                node.function_address = blockaddr_to_function[node.addr].addr


register_analysis(CFGFastSoot, 'CFGFastSoot')
