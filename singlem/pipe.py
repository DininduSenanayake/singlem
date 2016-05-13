import tempdir
import logging
import os.path
import shutil
import extern
import itertools
import tempfile
import subprocess
import json
import re
from string import split

from singlem import HmmDatabase, TaxonomyFile, OrfMUtils
from otu_table import OtuTable
from known_otu_table import KnownOtuTable
from metagenome_otu_finder import MetagenomeOtuFinder
from sequence_classes import SeqReader
from diamond_parser import DiamondResultParser

PPLACER_ASSIGNMENT_METHOD = 'pplacer'
DIAMOND_ASSIGNMENT_METHOD = 'diamond'
DIAMOND_EXAMPLE_BEST_HIT_ASSIGNMENT_METHOD = 'diamond_example'

class SearchPipe:
    def run(self, **kwargs):
        forward_read_files = kwargs.pop('sequences')
        output_otu_table = kwargs.pop('otu_table', None)
        archive_otu_table = kwargs.pop('archive_otu_table', None)
        num_threads = kwargs.pop('threads')
        known_otu_tables = kwargs.pop('known_otu_tables')
        singlem_assignment_method = kwargs.pop('assignment_method')
        output_jplace = kwargs.pop('output_jplace')
        output_extras = kwargs.pop('output_extras')
        evalue = kwargs.pop('evalue')
        min_orf_length = kwargs.pop('min_orf_length')
        restrict_read_length = kwargs.pop('restrict_read_length')
        filter_minimum = kwargs.pop('filter_minimum')
        include_inserts = kwargs.pop('include_inserts')
        singlem_packages = kwargs.pop('singlem_packages')

        working_directory = kwargs.pop('working_directory')
        force = kwargs.pop('force')
        if len(kwargs) > 0:
            raise Exception("Unexpected arguments detected: %s" % kwargs)
        
        self._num_threads = num_threads
        self._evalue = evalue
        self._min_orf_length = min_orf_length
        self._restrict_read_length = restrict_read_length
        self._filter_minimum = filter_minimum

        hmms = HmmDatabase(singlem_packages)
        if singlem_assignment_method == DIAMOND_EXAMPLE_BEST_HIT_ASSIGNMENT_METHOD:
            graftm_assignment_method = DIAMOND_ASSIGNMENT_METHOD
        else:
            graftm_assignment_method = singlem_assignment_method
            
        if logging.getLevelName(logging.getLogger().level) == 'DEBUG':
            self._graftm_verbosity = '5'
        else:
            self._graftm_verbosity = '2'

        using_temporary_working_directory = working_directory is None
        if using_temporary_working_directory:
            shared_mem_directory = '/dev/shm'
            if os.path.exists(shared_mem_directory):
                logging.debug("Using shared memory as a base directory")
                tmp = tempdir.TempDir(basedir=shared_mem_directory)
                tempfiles_path = os.path.join(tmp.name, 'tempfiles')
                os.mkdir(tempfiles_path)
                os.environ['TEMP'] = tempfiles_path
            else:
                logging.debug("Shared memory directory not detected, using default temporary directory instead")
                tmp = tempdir.TempDir()
            working_directory = tmp.name
        else:
            working_directory = working_directory
            if os.path.exists(working_directory):
                if force:
                    logging.info("Overwriting directory %s" % working_directory)
                    shutil.rmtree(working_directory)
                    os.mkdir(working_directory)
                else:
                    raise Exception("Working directory '%s' already exists, not continuing" % working_directory)
            else:
                os.mkdir(working_directory)
        logging.debug("Using working directory %s" % working_directory)
        self._working_directory = working_directory

        sample_to_gpkg_to_input_sequences = {}
        def return_cleanly():
            # remove these tempfiles because otherwise errors are spewed
            # when they are cleaned up after the tempdir is gone
            for gpkg_to_input_sequences in sample_to_gpkg_to_input_sequences.values():
                for seqs_tempfile in gpkg_to_input_sequences.values():
                    seqs_tempfile.close()
            if using_temporary_working_directory: tmp.dissolve()
            logging.info("Finished")

        #### Search
        self._singlem_package_database = hmms
        search_result = self._search(hmms, forward_read_files)
        sample_to_protein_hit_files = search_result.protein_hit_paths()
        sample_names = sample_to_protein_hit_files.keys()
        logging.debug("Recovered %i samples from GraftM search output e.g. %s" \
                     % (len(sample_names), sample_names[0]))
        if len(sample_names) == 0:
            logging.info("No reads identified in any samples, stopping")
            return_cleanly()
            return
        else:
            logging.debug("Found %i samples with reads identified" % len(sample_names))

        #### Alignment
        align_result = self._align(search_result)

        ### Extract reads
        sample_to_gpkg_to_input_sequences = self._extract_relevant_reads(
            align_result._graftm_separate_directory_base, sample_names, hmms, include_inserts)
        logging.info("Finished extracting aligned sequences")

        #### Taxonomic assignment
        logging.info("Running taxonomic assignment with graftm..")
        graftm_align_directory_base = os.path.join(working_directory, 'graftm_aligns')
        os.mkdir(graftm_align_directory_base)
        commands = []
        for sample_name in sample_names:
            if sample_name in sample_to_gpkg_to_input_sequences:
                for singlem_package in hmms:
                    key = singlem_package.graftm_package_basename()
                    if key in sample_to_gpkg_to_input_sequences[sample_name]:
                        tmp_graft = sample_to_gpkg_to_input_sequences[sample_name][key]
                        cmd = "graftM graft --threads %i --verbosity %s "\
                             "--min_orf_length %s "\
                             "--forward %s "\
                             "--graftm_package %s --output_directory %s/%s_vs_%s "\
                             "--input_sequence_type nucleotide "\
                             "--assignment_method %s" % (\
                                    1, #use 1 thread since most likely better to parallelise processes with extern, not threads here
                                    self._graftm_verbosity,
                                    min_orf_length,
                                    tmp_graft.name,
                                    singlem_package.graftm_package_path(),
                                    graftm_align_directory_base,
                                    sample_name,
                                    singlem_package.graftm_package_basename(),
                                    graftm_assignment_method)
                        if evalue: cmd += ' --evalue %s' % evalue
                        if restrict_read_length: cmd += ' --restrict_read_length %i' % restrict_read_length
                        if filter_minimum: cmd += '--filter_minimum %i' % filter_minimum
                        commands.append(cmd)
                    else:
                        logging.debug("No sequences found aligning from gpkg %s to sample %s, skipping" % (singlem_package.graftm_package_basename(), sample_name))
            else:
                logging.debug("No sequences found aligning to sample %s at all, skipping" % sample_name)
        extern.run_many(commands, num_threads=num_threads)
        logging.info("Finished running taxonomic assignment with graftm")

        #### Process taxonomically assigned reads
        # get the sequences out for each of them
        otu_table_object = OtuTable()
        regular_output_fields = split('gene sample sequence num_hits coverage taxonomy')
        otu_table_object.fields = regular_output_fields + split('read_names nucleotides_aligned taxonomy_by_known?')

        if known_otu_tables:
            logging.info("Parsing known taxonomy OTU tables")
            known_taxes = KnownOtuTable()
            known_taxes.parse_otu_tables(known_otu_tables)

        for sample_name in sample_names:
            if sample_name in sample_to_gpkg_to_input_sequences:
                for singlem_package in hmms:
                    key = singlem_package.graftm_package_basename()
                    if key in sample_to_gpkg_to_input_sequences[sample_name]:
                        tmp_graft = sample_to_gpkg_to_input_sequences[sample_name][key]

                        tmpbase = os.path.basename(tmp_graft.name[:-6])#remove .fasta
                        base_dir = os.path.join(graftm_align_directory_base,
                            '%s_vs_%s' % (sample_name, singlem_package.graftm_package_basename()),
                            tmpbase)

                        proteins_file = os.path.join(base_dir, "%s_orf.fa" % tmpbase)
                        nucleotide_file = os.path.join(base_dir, "%s_hits.fa" % tmpbase)
                        aligned_seqs = self._get_windowed_sequences(
                            proteins_file,
                            nucleotide_file, singlem_package.graftm_package().alignment_hmm_path(),
                            singlem_package.singlem_position(),
                            include_inserts)

                        if len(aligned_seqs) == 0:
                            logging.debug("Found no alignments for %s, skipping to next sample/hmm" % os.path.basename(singlem_package.graftm_package().alignment_hmm_path()))
                            continue
                        logging.debug("Found %i sequences for hmm %s, sample '%s'" % (len(aligned_seqs),
                                                                                    os.path.basename(singlem_package.graftm_package().alignment_hmm_path()),
                                                                                    sample_name))
                        if singlem_assignment_method == DIAMOND_EXAMPLE_BEST_HIT_ASSIGNMENT_METHOD:
                            tax_file = os.path.join(base_dir, '%s_diamond_assignment.daa' % tmpbase)
                        else:
                            tax_file = os.path.join(base_dir, "%s_read_tax.tsv" % tmpbase)
                        logging.debug("Reading taxonomy from %s" % tax_file)
                        
                        if singlem_assignment_method == DIAMOND_EXAMPLE_BEST_HIT_ASSIGNMENT_METHOD:
                            taxonomies = DiamondResultParser(tax_file)
                            use_first = True
                        else:
                            if not os.path.isfile(tax_file):
                                logging.warn("Unable to find tax file for gene %s from sample %s (likely do to min length filtering), skipping" %\
                                         (key, sample_name))
                                continue
                            taxonomies = TaxonomyFile(tax_file)
                            use_first = False

                        # convert to OTU table, output
                        infos = list(self._seqs_to_counts_and_taxonomy(aligned_seqs, taxonomies, use_first))
                        for info in infos:
                            if known_otu_tables:
                                tax_assigned_through_known = False
                            to_print = [singlem_package.graftm_package_basename(),
                                            sample_name,
                                            info.seq,
                                            info.count,
                                            info.coverage]
                            if known_otu_tables and info.seq in known_taxes:
                                tax_assigned_through_known = True
                                to_print.append(known_taxes[info.seq].taxonomy)
                            else:
                                to_print.append(info.taxonomy)

                            to_print.append(info.names)
                            to_print.append(info.aligned_lengths)
                            if known_otu_tables:
                                to_print.append(tax_assigned_through_known)
                            else:
                                to_print.append(False)
                            otu_table_object.data.append(to_print)
                            
                        if output_jplace:
                            input_jplace_file = os.path.join(base_dir, "placements.jplace")
                            output_jplace_file = os.path.join(base_dir, "%s_%s_%s.jplace" % (output_jplace, sample_name, singlem_package.graftm_package_basename()))
                            logging.debug("Converting jplace file %s to singlem jplace file %s" % (input_jplace_file, output_jplace_file))
                            with open(output_jplace_file, 'w') as output_jplace_io:
                                self._write_jplace_from_infos(open(input_jplace_file), infos, output_jplace_io)
                                
                            
        if output_otu_table:
            with open(output_otu_table, 'w') as f:
                if output_extras:
                    otu_table_object.write_to(f, otu_table_object.fields)
                else:
                    otu_table_object.write_to(f, regular_output_fields)
        if archive_otu_table:
            with open(archive_otu_table, 'w') as f:
                otu_table_object.archive(hmms.singlem_packages).write_to(f)
        return_cleanly()

    def _get_windowed_sequences(self, protein_sequences_file, nucleotide_sequence_file, hmm_path, position, include_inserts):
        if not os.path.exists(nucleotide_sequence_file) or \
            os.stat(nucleotide_sequence_file).st_size == 0: return []
        nucleotide_sequences = SeqReader().read_nucleotide_sequences(nucleotide_sequence_file)
        protein_alignment = self._align_proteins_to_hmm(protein_sequences_file,
                                                      hmm_path)
        return MetagenomeOtuFinder().find_windowed_sequences(protein_alignment,
                                                        nucleotide_sequences,
                                                        20,
                                                        include_inserts,
                                                        position)

    def _placement_input_fasta_name(self, hmm_and_position, sample_name, graftm_separate_directory_base):
        return '%s/%s_vs_%s/%s_hits/%s_hits_hits.fa' % (graftm_separate_directory_base,
                                                  sample_name,
                                                  os.path.basename(hmm_and_position.gpkg_path),
                                                  sample_name,
                                                  sample_name)

    def _extract_relevant_reads(self, graftm_separate_directory_base, sample_names, hmms, include_inserts):
        '''Given 'separates' directory, extract reads that will be used as
        part of the singlem choppage process as tempfiles in a hash'''
        sample_to_gpkg_to_input_sequences = {}
        for sample_name in sample_names:
            sample_to_gpkg_to_input_sequences[sample_name] = {}
            for singlem_package in hmms:
                base_dir = os.path.join(\
                    graftm_separate_directory_base,
                    "%s_vs_%s" % (sample_name,
                                  os.path.basename(singlem_package.graftm_package_path())),
                    '%s_hits' % sample_name)
                protein_sequences_file = os.path.join(
                    base_dir, "%s_hits_orf.fa" % sample_name)
                nucleotide_sequence_fasta_file = os.path.join(
                    base_dir, "%s_hits_hits.fa" % sample_name)

                # extract the names of the relevant reads
                aligned_seqs = self._get_windowed_sequences(\
                        protein_sequences_file,
                        nucleotide_sequence_fasta_file,
                        singlem_package.graftm_package().alignment_hmm_path(),
                        singlem_package.singlem_position(),
                        include_inserts)
                if len(aligned_seqs) > 0:
                    tmp = tempfile.NamedTemporaryFile(prefix='singlem.%s.' % sample_name,suffix='.fasta')
                    cmd = "fxtract -X -H -f /dev/stdin %s > %s" % (nucleotide_sequence_fasta_file, tmp.name)
                    process = subprocess.Popen(['bash','-c',cmd],
                                               stdin=subprocess.PIPE)
                    process.communicate("\n".join([s.name for s in aligned_seqs]))
                    sample_to_gpkg_to_input_sequences[sample_name][os.path.basename(singlem_package.graftm_package_path())] = tmp

        return sample_to_gpkg_to_input_sequences

    def _align_proteins_to_hmm(self, proteins_file, hmm_file):
        '''hmmalign proteins to hmm, and return an alignment object'''

        with tempfile.NamedTemporaryFile(prefix="singlem", suffix=".fasta") as f:
            cmd = "hmmalign %s %s |seqmagick convert --input-format stockholm - %s" % (hmm_file,
                                                              proteins_file,
                                                              f.name)
            extern.run(cmd)
            return SeqReader().protein_alignment_from_alignment_file(f.name)

    def _seqs_to_counts_and_taxonomy(self, sequences, taxonomies, use_first_taxonomy=False):
        '''given an array of Sequence objects, and hash of taxonomy file,
        yield over 'Info' objects that contain e.g. the counts of the aggregated
        sequences and corresponding median taxonomies.
        
        Parameters
        ----------
        use_first_taxonomy: boolean
            False: get a median taxonomy. True: use the taxonomy of the first encountered sequence 
        '''
        class CollectedInfo:
            def __init__(self):
                self.count = 0
                self.taxonomies = []
                self.names = []
                self.coverage = 0.0
                self.aligned_lengths = []

        seq_to_collected_info = {}
        for s in sequences:
            try:
                tax = taxonomies[s.name]
            except KeyError:
                # happens sometimes when HMMER picks up something where
                # diamond does not
                tax = ''
            try:
                collected_info = seq_to_collected_info[s.aligned_sequence]
            except KeyError:
                collected_info = CollectedInfo()
                seq_to_collected_info[s.aligned_sequence] = collected_info

            collected_info.count += 1
            if taxonomies: collected_info.taxonomies.append(tax)
            collected_info.names.append(s.name)
            collected_info.coverage += s.coverage_increment()
            collected_info.aligned_lengths.append(s.aligned_length)

        class Info:
            def __init__(self, seq, count, taxonomy, names, coverage, aligned_lengths):
                self.seq = seq
                self.count = count
                self.taxonomy = taxonomy
                self.names = names
                self.coverage = coverage
                self.aligned_lengths = aligned_lengths

        for seq, collected_info in seq_to_collected_info.iteritems():
            if use_first_taxonomy:
                tax = collected_info.taxonomies[0]
                if tax is None: tax = ''
            else:
                tax = self._median_taxonomy(collected_info.taxonomies)
            yield Info(seq,
                       collected_info.count,
                       tax,
                       collected_info.names,
                       collected_info.coverage,
                       collected_info.aligned_lengths)



    def _median_taxonomy(self, taxonomies):
        levels_to_counts = []
        for tax_string in taxonomies:
            for i, tax in enumerate(tax_string.split(';')):
                tax = tax.strip()
                if i >= len(levels_to_counts):
                    levels_to_counts.append({})
                try:
                    levels_to_counts[i][tax] += 1
                except KeyError:
                    levels_to_counts[i][tax] = 1


        median_tax = []
        for level_counts in levels_to_counts:
            max_count = 0
            max_tax = None
            for tax, count in level_counts.iteritems():
                if count > max_count:
                    max_count = count
                    max_tax = tax
            if float(max_count) / len(taxonomies) >= 0.5:
                median_tax.append(max_tax)
            else:
                break
        return '; '.join(median_tax)
    
    def _write_jplace_from_infos(self, input_jplace_io, infos, output_jplace_io):
        
        jplace = json.load(input_jplace_io)
        if jplace['version'] != 3:
            raise Exception("SingleM currently only works with jplace version 3 files, sorry")
        
        name_to_info = {}
        for info in infos:
            for name in info.names:
                name_to_info[name] = info
                
        # rewrite placements to be OTU-wise instead of sequence-wise
        orfm_utils = OrfMUtils()
        another_regex = re.compile(u'_\d+$')
        sequence_to_count = {}
        sequence_to_example_p = {}
        for placement in jplace['placements']:
            if 'nm' not in placement:
                raise Exception("Unexpected jplace format detected in placement %s" % placement)
            for name_and_count in placement['nm']:
                if len(name_and_count) != 2:
                    raise Exception("Unexpected jplace format detected in nm %s" % name_and_count)
                name, count = name_and_count
                real_name = another_regex.sub('', orfm_utils.un_orfm_name(name))
                info = name_to_info[real_name]
                sequence = info.seq
                
                try:
                    sequence_to_count[sequence] += count
                except KeyError:
                    sequence_to_count[sequence] = count
                    
                if real_name == info.names[0]:
                    sequence_to_example_p[sequence] = placement['p']
            
        new_placements = {}
        for sequence, example_p in sequence_to_example_p.items():
            new_placements[sequence] = {}
            new_placements[sequence]['nm'] = [[sequence, sequence_to_count[sequence]]]
            new_placements[sequence]['p'] = example_p
            
        jplace['placements'] = new_placements
        json.dump(jplace, output_jplace_io)
        
    def _search(self, singlem_package_database, forward_read_files):
        '''Find all reads that match one or more of the search HMMs in the
        singlem_package_database.

        Parameters
        ----------
        singlem_package_database: HmmDatabase
            packages to search the reads for
        forward_read_files: list of str
            paths to the sequences to be searched

        Returns
        -------
        SingleMPipeSearchResult
        '''
        graftm_search_directory = os.path.join(self._working_directory, 'graftm_search')
        # run graftm across all the HMMs
        logging.info("Using as input %i different sequence files e.g. %s" % (
            len(forward_read_files), forward_read_files[0]))
        cmd = "graftM graft --threads %i --forward %s "\
            "--min_orf_length %s "\
            "--search_hmm_files %s --search_and_align_only "\
            "--output_directory %s --aln_hmm_file %s --verbosity %s "\
            "--input_sequence_type nucleotide"\
                             % (self._num_threads,
                                ' '.join(forward_read_files),
                                self._min_orf_length,
                                ' '.join(singlem_package_database.search_hmm_paths()),
                                graftm_search_directory,
                                singlem_package_database.search_hmm_paths()[0],
                                self._graftm_verbosity)
        if self._evalue: cmd += ' --evalue %s' % self._evalue
        if self._restrict_read_length: cmd += ' --restrict_read_length %i' % self._restrict_read_length
        if self._filter_minimum: cmd += '--filter_minimum %i' % self._filter_minimum
        logging.info("Running GraftM to find particular reads..")
        extern.run(cmd)
        logging.info("Finished running GraftM search phase")

        return SingleMPipeSearchResult(graftm_search_directory)

    def _align(self, search_result):
        graftm_separate_directory_base = os.path.join(self._working_directory, 'graftm_separates')
        os.mkdir(graftm_separate_directory_base)
        logging.info("Running separate alignments in GraftM..")
        commands = []
        search_dict = search_result.protein_hit_paths()
        for sample_name, protein_hit_file in search_dict.items():
            for hmm in self._singlem_package_database:
                cmd = "graftM graft --threads %i --verbosity %s "\
                     "--min_orf_length %s "\
                     "--forward %s "\
                     "--graftm_package %s --output_directory %s/%s_vs_%s "\
                     "--input_sequence_type nucleotide "\
                     "--search_and_align_only" % (\
                            1, #use 1 thread since most likely better to parallelise processes with extern, not threads here
                            self._graftm_verbosity,
                            self._min_orf_length,
                            protein_hit_file,
                            hmm.graftm_package_path(),
                            graftm_separate_directory_base,
                            sample_name,
                            os.path.basename(hmm.graftm_package_path()))
                if self._evalue: cmd += ' --evalue %s' % self._evalue
                if self._restrict_read_length:
                    cmd += ' --restrict_read_length %i' % self._restrict_read_length
                if self._filter_minimum: cmd += '--filter_minimum %i' % self._filter_minimum
                commands.append(cmd)
        extern.run_many(commands, num_threads=self._num_threads)
        return SingleMPipeAlignSearchResult(
            graftm_separate_directory_base, search_dict.keys())


class SingleMPipeSearchResult:
    def __init__(self, graftm_output_directory):
        self._graftm_output_directory = graftm_output_directory

    def protein_hit_paths(self):
        '''Return a dict of sample name to corresponding '_hits.fa' files generated in
        the search step. Do not return those samples where there were no hits.

        '''
        sample_names = [f for f in os.listdir(self._graftm_output_directory) \
                        if os.path.isdir(os.path.join(self._graftm_output_directory, f))]
        paths = {}
        for sample in sample_names:
            path = "%s/%s/%s_hits.fa" % (
                self._graftm_output_directory, sample, sample)
            if os.stat(path).st_size > 0:
                paths[sample] = path
        return paths

class SingleMPipeAlignSearchResult:
    def __init__(self, graftm_separate_directory_base, sample_names):
        self._graftm_separate_directory_base = graftm_separate_directory_base
        self._sample_names = sample_names
